"""Third-party SDK seam requiring one explicit logical invocation per POST.

An invocation context may cover exactly one HTTP dispatch. Hidden SDK/application
retries fail locally; use the lean receipt client for recovery instead.
"""

import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx

from .http import GatewayError, TokenSource, endpoint_url, identifier


class GatewayBindingError(GatewayError):
    pass


@dataclass
class _Invocation:
    request_id: str
    used: bool = False
    lock: object = field(default_factory=threading.Lock, repr=False)


_active = ContextVar("gateway_logical_invocation", default=None)


@contextmanager
def gateway_invocation(request_id=None):
    """Wrap one SDK create/respond call; retain this ID if receipt recovery is needed."""
    invocation = _Invocation(identifier(request_id or uuid.uuid4().hex))
    marker = _active.set(invocation)
    try:
        yield invocation.request_id
    finally:
        _active.reset(marker)


class _Boundary:
    def __init__(self, base_url, *, token=None, token_file=None, unix_socket=None):
        self.origin = endpoint_url(base_url)
        self.tokens = TokenSource(token, token_file, unix_socket=unix_socket)

    def guard(self, request):
        invocation = _active.get()
        if invocation is None:
            raise GatewayBindingError("logical_invocation_required_migration_blocked")
        if request.method != "POST" or str(request.url) not in {
            self.origin + "/v1/responses",
            self.origin + "/v1/chat/completions",
            self.origin + "/v1/embeddings",
        }:
            raise GatewayBindingError("gateway_sdk_destination_denied")
        if request.headers.get("x-stainless-retry-count") not in {None, "0"}:
            raise GatewayBindingError("sdk_retries_must_be_disabled_migration_blocked")
        existing = request.headers.get("idempotency-key")
        if existing and existing != invocation.request_id:
            raise GatewayBindingError("static_or_conflicting_idempotency_key_migration_blocked")
        with invocation.lock:
            if invocation.used:
                raise GatewayBindingError("repeated_sdk_dispatch_denied_query_receipt")
            invocation.used = True
        request.headers["Idempotency-Key"] = invocation.request_id
        request.headers["Accept-Encoding"] = "identity"
        request.headers.pop("Authorization", None)
        request.headers.update(self.tokens.headers())


class GatewayOpenAITransport(httpx.BaseTransport):
    def __init__(self, base_url, *, token=None, token_file=None, unix_socket=None, transport=None):
        self.boundary = _Boundary(
            base_url, token=token, token_file=token_file, unix_socket=unix_socket
        )
        self.transport = transport or httpx.HTTPTransport(
            retries=0, trust_env=False, uds=str(unix_socket) if unix_socket else None
        )

    def handle_request(self, request):
        self.boundary.guard(request)
        response = self.transport.handle_request(request)
        if response.headers.get("content-encoding", "identity") not in {"identity", ""}:
            response.close()
            raise GatewayBindingError("compressed_gateway_response_denied_query_receipt")
        return response

    def close(self):
        self.transport.close()


class AsyncGatewayOpenAITransport(httpx.AsyncBaseTransport):
    def __init__(self, base_url, *, token=None, token_file=None, unix_socket=None, transport=None):
        self.boundary = _Boundary(
            base_url, token=token, token_file=token_file, unix_socket=unix_socket
        )
        self.transport = transport or httpx.AsyncHTTPTransport(
            retries=0, trust_env=False, uds=str(unix_socket) if unix_socket else None
        )

    async def handle_async_request(self, request):
        self.boundary.guard(request)
        response = await self.transport.handle_async_request(request)
        if response.headers.get("content-encoding", "identity") not in {"identity", ""}:
            await response.aclose()
            raise GatewayBindingError("compressed_gateway_response_denied_query_receipt")
        return response

    async def aclose(self):
        await self.transport.aclose()
