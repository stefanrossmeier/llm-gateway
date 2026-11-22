"""Lean sync/async failure boundaries; no service SDK or ASGI dependency."""

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from llm_gateway.client import (
    AsyncGatewayClient,
    GatewayClient,
    GatewayError,
    GatewayHTTPError,
    GatewayProtocolError,
    GatewayTransportError,
)
from llm_gateway.client import http as client_http
from llm_gateway.contracts import ChatInput, InferenceRequest

pytestmark = pytest.mark.offline
CLIENTS = [GatewayClient, AsyncGatewayClient]


def request():
    return InferenceRequest(
        request_id="stable-id",
        alias="summary",
        operation="chat",
        input=ChatInput(messages=[{"role": "user", "content": "public"}]),
    )


@asynccontextmanager
async def opened(cls, **kwargs):
    if cls is AsyncGatewayClient:
        async with cls("https://gateway.test", **kwargs) as client:
            yield client
    else:
        with cls("https://gateway.test", **kwargs) as client:
            yield client


async def infer(client):
    if isinstance(client, AsyncGatewayClient):
        return await client.infer(request())
    return client.infer(request())


@pytest.mark.parametrize("cls", CLIENTS)
@pytest.mark.parametrize(
    "token", ["\ud800" * 32, "é" * 32, "a" * 31, "a" * 16385, "a" * 32 + "\n", "a" * 32 + " ", 123]
)
async def test_invalid_inline_token_has_safe_exception_before_transport(cls, token):
    async with opened(
        cls,
        token=token,
        transport=httpx.MockTransport(lambda req: pytest.fail("invalid token reached transport")),
    ) as client:
        with pytest.raises(GatewayError) as caught:
            await infer(client)
        assert str(caught.value) == "gateway_token_invalid"


@pytest.mark.parametrize("cls", CLIENTS)
@pytest.mark.parametrize("timeout", [0, -1, 3611, float("inf"), float("nan"), True, None, "60"])
def test_invalid_timeout_has_safe_exception_before_transport(cls, timeout):
    with pytest.raises(GatewayError, match="gateway_timeout_invalid"):
        cls("https://gateway.test", token="a" * 48, timeout_seconds=timeout)


@pytest.mark.parametrize("cls", CLIENTS)
@pytest.mark.parametrize(
    "receipts", [None, 7, "invalid", [None], [{"measured_usd": "secret-body"}]]
)
async def test_malformed_error_receipts_do_not_escape_or_hide_http_identity(cls, receipts):
    calls = []

    def send(req):
        calls.append(req)
        return httpx.Response(
            503,
            json={"receipts": receipts, "error": "secret-body"},
            headers={
                "X-Gateway-Trace-Id": "safe-trace",
                "X-Gateway-Receipt-Id": "invalid/path",
            },
        )

    async with opened(cls, token="a" * 48, transport=httpx.MockTransport(send)) as client:
        with pytest.raises(GatewayHTTPError) as caught:
            await infer(client)
    error = caught.value
    assert (
        error.status == 503 and error.request_id == "stable-id" and error.trace_id == "safe-trace"
    )
    assert error.receipt_id is None and not error.receipts
    assert "secret-body" not in repr(error) and "a" * 48 not in repr(error)
    assert len(calls) == 1


@pytest.mark.parametrize("cls", CLIENTS)
@pytest.mark.parametrize(
    "status,body,error",
    [
        (503, b"secret non-JSON body", GatewayHTTPError),
        (200, b"secret non-JSON body", GatewayProtocolError),
        (200, b"{}", GatewayProtocolError),
        (503, b'{"execution_status":"bogus","secret":"body"}', GatewayProtocolError),
    ],
)
async def test_invalid_reply_uses_safe_typed_error_and_never_retries(cls, status, body, error):
    calls = []

    def send(req):
        calls.append(req)
        return httpx.Response(status, content=body)

    async with opened(cls, token="a" * 48, transport=httpx.MockTransport(send)) as client:
        with pytest.raises(error) as caught:
            await infer(client)
    assert caught.value.request_id == "stable-id"
    assert "secret" not in repr(caught.value) and len(calls) == 1


@pytest.mark.parametrize("cls", CLIENTS)
@pytest.mark.parametrize("kind", ["compressed", "oversize", "transport"])
async def test_response_resource_and_transport_failures_remain_single_post(cls, kind, monkeypatch):
    calls = []
    monkeypatch.setattr(client_http, "MAX_RESPONSE", 8)

    def send(req):
        calls.append(req)
        if kind == "transport":
            raise httpx.ReadError("secret upstream failure")
        if kind == "compressed":
            return httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"unread")
            )
        return httpx.Response(200, content=b"x" * 9)

    async with opened(cls, token="a" * 48, transport=httpx.MockTransport(send)) as client:
        with pytest.raises(
            GatewayTransportError if kind == "transport" else GatewayProtocolError
        ) as caught:
            await infer(client)
    assert caught.value.request_id == "stable-id" and len(calls) == 1
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("cls", CLIENTS)
async def test_missing_private_token_and_ambiguous_authority_fail_before_http(cls, tmp_path):
    with pytest.raises(GatewayError, match="ambiguous_gateway_token"):
        cls("https://gateway.test", token="a" * 48, token_file=tmp_path / "unused")
    with pytest.raises(GatewayError, match="gateway_token_required_for_tcp"):
        cls("https://gateway.test")
    async with opened(
        cls,
        token_file=tmp_path / "missing",
        transport=httpx.MockTransport(lambda req: pytest.fail("missing token reached transport")),
    ) as client:
        with pytest.raises(GatewayError, match="gateway_token_unavailable"):
            await infer(client)


@pytest.mark.parametrize("cls", CLIENTS)
@pytest.mark.parametrize("identifier", [".", "..", "../other", "", None])
async def test_invalid_lookup_identifier_cannot_change_the_path(cls, identifier):
    async with opened(
        cls,
        token="a" * 48,
        transport=httpx.MockTransport(lambda req: pytest.fail("invalid path reached transport")),
    ) as client:
        with pytest.raises(GatewayError, match="invalid_gateway_identifier"):
            if isinstance(client, AsyncGatewayClient):
                await client.get_request(identifier)
            else:
                client.get_request(identifier)


@pytest.mark.parametrize("cls", CLIENTS)
async def test_error_without_canonical_request_preserves_safe_header_id(cls):
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            403,
            content=json.dumps({"error": "private"}).encode(),
            headers={
                "X-Gateway-Request-Id": "original",
                "X-Gateway-Trace-Id": "unsafe/path",
            },
        )
    )
    async with opened(cls, token="a" * 48, transport=transport) as client:
        with pytest.raises(GatewayHTTPError) as caught:
            if isinstance(client, AsyncGatewayClient):
                await client.ready()
            else:
                client.ready()
    assert caught.value.request_id == "original" and caught.value.trace_id is None


@pytest.mark.parametrize("cls", CLIENTS)
@pytest.mark.parametrize("kind", ["relative", "missing", "regular", "symlink"])
def test_unverified_unix_path_cannot_bypass_gateway_authentication(cls, kind, tmp_path):
    path = tmp_path / "socket"
    if kind == "relative":
        path = "relative/socket"
    elif kind in {"regular", "symlink"}:
        leaf = tmp_path / "regular"
        leaf.touch(mode=0o600)
        if kind == "regular":
            path = leaf
        else:
            path.symlink_to(leaf)
    with pytest.raises(GatewayError, match="gateway_unix_socket_not_private"):
        cls("http://gateway.test", unix_socket=path)
