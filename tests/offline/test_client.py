import json

import httpx
import pytest
from test_ledger import request

from llm_gateway.client import (
    AsyncGatewayClient,
    GatewayClient,
    GatewayHTTPError,
    GatewayTransportError,
)
from llm_gateway.client.http import GatewayError
from llm_gateway.contracts import ChatInput, Message

pytestmark = pytest.mark.offline
pytest_plugins = ["test_api"]


def test_sync_client_typed_inference_receipts_usage_and_token_rotation(service):
    server, _, token_path, _ = service

    def send(req):
        return server.request(
            req.method, req.url.raw_path.decode(), content=req.content, headers=req.headers
        )

    with GatewayClient(
        "http://gateway.test", token_file=token_path, transport=httpx.MockTransport(send)
    ) as client:
        result = client.infer(request())
        assert result.content == "first"
        assert client.receipt(result.receipts[0].receipt_id).evidence_status == "simulated"
        assert client.get_request("one") == result
        assert client.usage(group_by="alias").groups["summary"].attempts == 1
        assert client.aliases().aliases[0].alias == "summary"
        token_path.write_text("n" * 48)
        assert client.ready().status == "ready"
        assert client.cancel("one")["status"] == "cancellation_requested"


async def test_async_client_and_convenience_method(service):
    server, _, _, _ = service
    async with AsyncGatewayClient(
        "http://gateway.test", token="a" * 48, transport=httpx.ASGITransport(app=server.app)
    ) as client:
        result = await client.chat(
            "summary",
            ChatInput(messages=[Message(role="user", content="public")]),
            request_id="async-one",
        )
        assert result.content == "first"
        assert (await client.receipt(result.receipts[0].receipt_id)).usage.input_tokens is None
        assert (await client.get_request("async-one")).content == "first"
        assert (await client.usage()).attempts == 1


def test_transport_failure_never_retries_and_preserves_generated_id():
    calls = []

    def fail(req):
        calls.append(req)
        raise httpx.ReadTimeout("secret provider text must not escape", request=req)

    with GatewayClient(
        "https://gateway.test", token="a" * 48, transport=httpx.MockTransport(fail)
    ) as client:
        with pytest.raises(GatewayTransportError) as caught:
            client.chat("summary", ChatInput(messages=[Message(role="user", content="public")]))
    assert len(calls) == 1 and caught.value.request_id
    assert calls[0].headers["Idempotency-Key"] == caught.value.request_id
    assert json.loads(calls[0].content)["request_id"] == caught.value.request_id
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("status", [302, 429, 503])
def test_http_errors_never_follow_or_retry_or_echo_bodies(status):
    calls = []

    def fail(req):
        calls.append(req)
        return httpx.Response(
            status, json={"error": "a" * 48}, headers={"Location": "https://evil.invalid"}
        )

    with GatewayClient(
        "https://gateway.test", token="a" * 48, transport=httpx.MockTransport(fail)
    ) as client:
        with pytest.raises(GatewayHTTPError) as caught:
            client.infer(request())
    assert len(calls) == 1 and caught.value.request_id == "one"
    assert caught.value.status == status and "a" * 48 not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@gateway.test",
        "https://gateway.test/path",
        "file:///tmp/key",
        "https://gateway.test?api_key=secret",
    ],
)
def test_explicit_gateway_origin_has_no_credentials_query_or_discovery(url):
    with pytest.raises(GatewayError, match="explicit_origin"):
        GatewayClient(url, token="a" * 48)


def test_owned_tls_context_ignores_ambient_certificate_override(monkeypatch):
    from llm_gateway.client import GatewayOpenAITransport

    monkeypatch.setenv("SSL_CERT_FILE", "/missing/untrusted-ca.pem")
    with GatewayClient("https://gateway.test", token="a" * 48):
        pass
    GatewayOpenAITransport("https://gateway.test", token="a" * 48).close()


def test_compressed_response_is_rejected_before_reading_it():
    from llm_gateway.client import GatewayProtocolError

    class MustNotRead(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("compressed response was read")

    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=MustNotRead())
    )
    with GatewayClient("https://gateway.test", token="a" * 48, transport=transport) as client:
        with pytest.raises(GatewayProtocolError) as caught:
            client.infer(request())
    assert caught.value.request_id == "one"
