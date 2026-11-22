"""Lean, bounded clients: one POST per call, explicit receipt recovery on failure."""

import json
import math
import os
import re
import stat
import uuid
from collections.abc import Coroutine
from typing import Any, Generic, TypeVar

import httpx
from pydantic import ValidationError

from llm_gateway.contracts import (
    AttemptReceipt,
    CallerLimits,
    ChatInput,
    DecisionsInput,
    EmbeddingsInput,
    InferenceRequest,
    InferenceResult,
    RequestContext,
    ResponsesInput,
    XSearchInput,
)
from llm_gateway.contracts.reports import AliasReport, Readiness, UsageReport
from llm_gateway.credentials import read_private_secret

MAX_RESPONSE = 32 * 1024 * 1024


class GatewayError(Exception):
    """Safe gateway failure; no raw response/headers or secret values in repr."""


class GatewayTransportError(GatewayError):
    def __init__(self, request_id=None):
        self.request_id = request_id
        super().__init__("gateway_transport_failed_query_receipt")


class GatewayProtocolError(GatewayError):
    def __init__(self, request_id=None):
        self.request_id = request_id
        super().__init__("gateway_response_invalid_query_receipt")


class GatewayHTTPError(GatewayError):
    def __init__(self, status, *, request_id=None, receipt_id=None, trace_id=None, receipts=()):
        self.status, self.request_id, self.receipt_id, self.trace_id = (
            status,
            request_id,
            receipt_id,
            trace_id,
        )
        self.receipts = tuple(receipts)
        super().__init__(f"gateway_http_{status}_query_receipt")


def endpoint_url(base_url):
    try:
        url = httpx.URL(base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError()
        return str(url.copy_with(path="", query=None, fragment=None)).rstrip("/")
    except (ValueError, TypeError, httpx.InvalidURL):
        raise GatewayError("gateway_base_url_requires_explicit_origin") from None


def identifier(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)
        or value in {".", ".."}
    ):
        raise GatewayError("invalid_gateway_identifier")
    return value


class TokenSource:
    def __init__(self, token=None, token_file=None, *, unix_socket=None):
        if token is not None and token_file is not None:
            raise GatewayError("ambiguous_gateway_token")
        if token is None and token_file is None and unix_socket is None:
            raise GatewayError("gateway_token_required_for_tcp")
        self._token, self._file = token, token_file
        if unix_socket is not None:
            try:
                from pathlib import Path

                from llm_gateway.storage.sqlite import verify_private_ancestors

                path = Path(unix_socket)
                if not path.is_absolute():
                    raise ValueError()
                verify_private_ancestors(path.parent)
                info = os.stat(unix_socket, follow_symlinks=False)
                if (
                    not stat.S_ISSOCK(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    raise ValueError()
            except (OSError, ValueError):
                raise GatewayError("gateway_unix_socket_not_private") from None

    def headers(self):
        try:
            token = (
                read_private_secret(os.fspath(self._file))
                if self._file is not None
                else self._token
            )
        except ValueError:
            raise GatewayError("gateway_token_unavailable") from None
        if token is None:
            return {}
        if (
            not isinstance(token, str)
            or not token.isascii()
            or not 32 <= len(token) <= 16384
            or "\n" in token
            or "\r" in token
            or any(ord(char) <= 32 or ord(char) >= 127 for char in token)
        ):
            raise GatewayError("gateway_token_invalid")
        return {"Authorization": "Bearer " + token}


def _decode(response, content, model, *, request_id=None):
    try:
        data = json.loads(content)
    except (ValueError, UnicodeError):
        if response.is_error or response.is_redirect:
            raise GatewayHTTPError(response.status_code, request_id=request_id) from None
        raise GatewayProtocolError(request_id) from None
    if model is InferenceResult and isinstance(data, dict) and "execution_status" in data:
        try:
            return model.model_validate(data)
        except ValidationError:
            raise GatewayProtocolError(request_id) from None
    if not response.is_success:

        def safe_header(name):
            value = response.headers.get(name)
            return value if value and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else None

        try:
            receipts = (
                [AttemptReceipt.model_validate(r) for r in data.get("receipts", [])]
                if isinstance(data, dict)
                else []
            )
        except (ValidationError, TypeError):
            receipts = []
        raise GatewayHTTPError(
            response.status_code,
            request_id=request_id or safe_header("X-Gateway-Request-Id"),
            receipt_id=safe_header("X-Gateway-Receipt-Id"),
            trace_id=safe_header("X-Gateway-Trace-Id"),
            receipts=receipts,
        )
    try:
        return model.model_validate(data) if model is not None else data
    except ValidationError:
        raise GatewayProtocolError(request_id) from None


def make_request(alias, data, *, request_id=None, context=None, limits=None):
    return InferenceRequest(
        request_id=identifier(request_id or uuid.uuid4().hex),
        alias=alias,
        operation=data.kind,
        input=data,
        context=context or RequestContext(),
        limits=limits or CallerLimits(),
    )


_Result = TypeVar("_Result")


class _Methods(Generic[_Result]):
    def infer(
        self, request: InferenceRequest, *, expected_config_digest: str | None = None
    ) -> _Result:
        raise NotImplementedError

    # These return the synchronous result or the async client's awaitable. No
    # request/ID is regenerated once infer starts, including transport failure.
    def chat(
        self,
        alias: str,
        data: ChatInput,
        *,
        request_id: str | None = None,
        context: RequestContext | None = None,
        limits: CallerLimits | None = None,
    ) -> _Result:
        return self.infer(
            make_request(alias, data, request_id=request_id, context=context, limits=limits)
        )

    def responses(
        self,
        alias: str,
        data: ResponsesInput,
        *,
        request_id: str | None = None,
        context: RequestContext | None = None,
        limits: CallerLimits | None = None,
    ) -> _Result:
        return self.infer(
            make_request(alias, data, request_id=request_id, context=context, limits=limits)
        )

    def decisions(
        self,
        alias: str,
        data: DecisionsInput,
        *,
        request_id: str | None = None,
        context: RequestContext | None = None,
        limits: CallerLimits | None = None,
    ) -> _Result:
        return self.infer(
            make_request(alias, data, request_id=request_id, context=context, limits=limits)
        )

    def x_search(
        self,
        alias: str,
        data: XSearchInput,
        *,
        request_id: str | None = None,
        context: RequestContext | None = None,
        limits: CallerLimits | None = None,
    ) -> _Result:
        return self.infer(
            make_request(alias, data, request_id=request_id, context=context, limits=limits)
        )

    def embeddings(
        self,
        alias: str,
        data: EmbeddingsInput,
        *,
        request_id: str | None = None,
        context: RequestContext | None = None,
        limits: CallerLimits | None = None,
    ) -> _Result:
        return self.infer(
            make_request(alias, data, request_id=request_id, context=context, limits=limits)
        )


class GatewayClient(_Methods[InferenceResult]):
    def __init__(
        self,
        base_url: str,
        *,
        token=None,
        token_file=None,
        unix_socket=None,
        timeout_seconds=3610,
        transport=None,
    ):
        self.base_url = endpoint_url(base_url)
        self.tokens = TokenSource(token, token_file, unix_socket=unix_socket)
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3610
        ):
            raise GatewayError("gateway_timeout_invalid")
        self._http = httpx.Client(
            transport=transport
            or httpx.HTTPTransport(
                retries=0, trust_env=False, uds=os.fspath(unix_socket) if unix_socket else None
            ),
            headers={"Accept-Encoding": "identity"},
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds, connect=5, write=30, pool=10),
        )

    def _call(
        self,
        method,
        path,
        model=None,
        *,
        data=None,
        request_id=None,
        params=None,
        expected_config_digest=None,
    ):
        headers = self.tokens.headers()
        if expected_config_digest is not None:
            if not isinstance(expected_config_digest, str) or not re.fullmatch(
                r"[a-f0-9]{64}", expected_config_digest
            ):
                raise GatewayError("expected_configuration_digest_invalid")
            headers["X-Gateway-Expected-Config-Digest"] = expected_config_digest
        if request_id:
            headers["Idempotency-Key"] = identifier(request_id)
        try:
            with self._http.stream(
                method, self.base_url + path, json=data, params=params, headers=headers
            ) as response:
                if response.headers.get("content-encoding", "identity") not in {"identity", ""}:
                    raise GatewayProtocolError(request_id)
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE:
                        raise GatewayProtocolError(request_id)
                return _decode(response, content, model, request_id=request_id)
        except httpx.HTTPError:
            raise GatewayTransportError(request_id) from None

    def infer(
        self, request: InferenceRequest, *, expected_config_digest: str | None = None
    ) -> InferenceResult:
        return self._call(
            "POST",
            "/v1/inferences",
            InferenceResult,
            data=request.model_dump(mode="json"),
            request_id=request.request_id,
            expected_config_digest=expected_config_digest,
        )

    def get_request(self, request_id: str) -> InferenceResult:
        return self._call(
            "GET", "/v1/requests/" + identifier(request_id), InferenceResult, request_id=request_id
        )

    def receipt(self, receipt_id: str) -> AttemptReceipt:
        return self._call("GET", "/v1/receipts/" + identifier(receipt_id), AttemptReceipt)

    def cancel(self, request_id: str):
        return self._call("POST", "/v1/requests/" + identifier(request_id) + "/cancel")

    def usage(self, *, filters=None, group_by=None) -> UsageReport:
        return self._call(
            "GET",
            "/v1/usage",
            UsageReport,
            params={**(filters or {}), **({"group_by": group_by} if group_by else {})},
        )

    def aliases(self) -> AliasReport:
        return self._call("GET", "/v1/aliases", AliasReport)

    def ready(self) -> Readiness:
        return self._call("GET", "/ready", Readiness)

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class AsyncGatewayClient(_Methods[Coroutine[Any, Any, InferenceResult]]):
    def __init__(
        self,
        base_url: str,
        *,
        token=None,
        token_file=None,
        unix_socket=None,
        timeout_seconds=3610,
        transport=None,
    ):
        self.base_url = endpoint_url(base_url)
        self.tokens = TokenSource(token, token_file, unix_socket=unix_socket)
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3610
        ):
            raise GatewayError("gateway_timeout_invalid")
        self._http = httpx.AsyncClient(
            transport=transport
            or httpx.AsyncHTTPTransport(
                retries=0, trust_env=False, uds=os.fspath(unix_socket) if unix_socket else None
            ),
            headers={"Accept-Encoding": "identity"},
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds, connect=5, write=30, pool=10),
        )

    async def _call(
        self,
        method,
        path,
        model=None,
        *,
        data=None,
        request_id=None,
        params=None,
        expected_config_digest=None,
    ):
        headers = self.tokens.headers()
        if expected_config_digest is not None:
            if not isinstance(expected_config_digest, str) or not re.fullmatch(
                r"[a-f0-9]{64}", expected_config_digest
            ):
                raise GatewayError("expected_configuration_digest_invalid")
            headers["X-Gateway-Expected-Config-Digest"] = expected_config_digest
        if request_id:
            headers["Idempotency-Key"] = identifier(request_id)
        try:
            async with self._http.stream(
                method, self.base_url + path, json=data, params=params, headers=headers
            ) as response:
                if response.headers.get("content-encoding", "identity") not in {"identity", ""}:
                    raise GatewayProtocolError(request_id)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE:
                        raise GatewayProtocolError(request_id)
                return _decode(response, content, model, request_id=request_id)
        except httpx.HTTPError:
            raise GatewayTransportError(request_id) from None

    async def infer(
        self, request: InferenceRequest, *, expected_config_digest: str | None = None
    ) -> InferenceResult:
        return await self._call(
            "POST",
            "/v1/inferences",
            InferenceResult,
            data=request.model_dump(mode="json"),
            request_id=request.request_id,
            expected_config_digest=expected_config_digest,
        )

    async def get_request(self, request_id: str) -> InferenceResult:
        return await self._call(
            "GET", "/v1/requests/" + identifier(request_id), InferenceResult, request_id=request_id
        )

    async def receipt(self, receipt_id: str) -> AttemptReceipt:
        return await self._call("GET", "/v1/receipts/" + identifier(receipt_id), AttemptReceipt)

    async def cancel(self, request_id: str):
        return await self._call("POST", "/v1/requests/" + identifier(request_id) + "/cancel")

    async def usage(self, *, filters=None, group_by=None) -> UsageReport:
        return await self._call(
            "GET",
            "/v1/usage",
            UsageReport,
            params={**(filters or {}), **({"group_by": group_by} if group_by else {})},
        )

    async def aliases(self) -> AliasReport:
        return await self._call("GET", "/v1/aliases", AliasReport)

    async def ready(self) -> Readiness:
        return await self._call("GET", "/ready", Readiness)

    async def aclose(self):
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()
