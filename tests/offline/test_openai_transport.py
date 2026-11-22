import json

import httpx
import pytest

from llm_gateway.client.openai_transport import (
    AsyncGatewayOpenAITransport,
    GatewayBindingError,
    GatewayOpenAITransport,
    gateway_invocation,
)

pytestmark = pytest.mark.offline


def test_actual_sdk_requires_one_logical_id_and_overwrites_sdk_credential():
    from openai import APIConnectionError, OpenAI

    calls = []

    def send(req):
        calls.append(req)
        return httpx.Response(
            200,
            json={
                "id": "chat-fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
            },
        )

    transport = GatewayOpenAITransport(
        "https://gateway.test", token="g" * 48, transport=httpx.MockTransport(send)
    )
    with httpx.Client(transport=transport) as http:
        sdk = OpenAI(
            base_url="https://gateway.test/v1",
            api_key="gateway-managed",
            http_client=http,
            max_retries=0,
        )
        with gateway_invocation("logical-one"):
            assert (
                sdk.chat.completions.create(
                    model="summary", messages=[{"role": "user", "content": "public"}]
                )
                .choices[0]
                .message.content
                == "ok"
            )
        assert calls[0].headers["Authorization"] == "Bearer " + "g" * 48
        assert calls[0].headers["Idempotency-Key"] == "logical-one"
        with pytest.raises(APIConnectionError) as caught:
            sdk.chat.completions.create(
                model="summary", messages=[{"role": "user", "content": "public"}]
            )
        assert "logical_invocation_required" in str(caught.value.__cause__)
        assert len(calls) == 1


def test_hidden_sdk_retries_are_blocked_before_another_dispatch(monkeypatch):
    import openai._base_client
    from openai import APIConnectionError, OpenAI

    monkeypatch.setattr(openai._base_client.time, "sleep", lambda _: None)
    calls = []

    def fail(req):
        calls.append(req)
        return httpx.Response(503, json={"error": {"message": "synthetic unavailable"}})

    with httpx.Client(
        transport=GatewayOpenAITransport(
            "https://gateway.test", token="g" * 48, transport=httpx.MockTransport(fail)
        )
    ) as http:
        sdk = OpenAI(
            base_url="https://gateway.test/v1",
            api_key="gateway-managed",
            http_client=http,
            max_retries=2,
        )
        with gateway_invocation("logical-one"), pytest.raises(APIConnectionError):
            sdk.chat.completions.create(
                model="summary", messages=[{"role": "user", "content": "public"}]
            )
    assert len(calls) == 1


def test_static_key_second_dispatch_and_destination_changes_fail_locally():
    calls = []
    transport = GatewayOpenAITransport(
        "https://gateway.test",
        token="g" * 48,
        transport=httpx.MockTransport(
            lambda req: calls.append(req) or httpx.Response(200, json={})
        ),
    )
    with httpx.Client(transport=transport) as client:
        with (
            gateway_invocation("one"),
            pytest.raises(GatewayBindingError, match="static_or_conflicting"),
        ):
            client.post(
                "https://gateway.test/v1/responses", headers={"Idempotency-Key": "static"}, json={}
            )
        with gateway_invocation("one"):
            client.post("https://gateway.test/v1/responses", json={})
            with pytest.raises(GatewayBindingError, match="repeated_sdk_dispatch"):
                client.post("https://gateway.test/v1/responses", json={})
        with (
            gateway_invocation("two"),
            pytest.raises(GatewayBindingError, match="destination_denied"),
        ):
            client.post("https://evil.invalid/v1/responses", json={})
    assert len(calls) == 1


async def test_async_transport_is_independent_per_invocation():
    calls = []

    async def send(req):
        calls.append(req)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=AsyncGatewayOpenAITransport(
            "https://gateway.test", token="g" * 48, transport=httpx.MockTransport(send)
        )
    ) as client:
        with gateway_invocation("one"):
            await client.post(
                "https://gateway.test/v1/embeddings", json={"model": "embedding-alias"}
            )
        with gateway_invocation("two"):
            await client.post("https://gateway.test/v1/responses", json={"model": "answer-alias"})
    assert [r.headers["Idempotency-Key"] for r in calls] == ["one", "two"]
    assert [json.loads(r.content)["model"] for r in calls] == ["embedding-alias", "answer-alias"]


def test_sdk_compression_is_rejected_before_reading_or_retrying():
    class MustNotRead(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("compressed response was read")

    transport = GatewayOpenAITransport(
        "https://gateway.test",
        token="g" * 48,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, stream=MustNotRead()
            )
        ),
    )
    with httpx.Client(transport=transport) as client, gateway_invocation("one"):
        with pytest.raises(GatewayBindingError, match="compressed_gateway_response_denied"):
            client.post("https://gateway.test/v1/responses", json={})
