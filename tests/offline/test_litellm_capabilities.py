"""T008: actual pinned adapter transformations, no mock_response shortcut or network."""

import importlib.metadata
import json

import httpx
import pytest

pytestmark = pytest.mark.offline


@pytest.fixture
def transport(monkeypatch):
    import litellm

    assert importlib.metadata.version("litellm") == "1.109.0"
    litellm.telemetry = False
    litellm.success_callback = []
    litellm.failure_callback = []
    litellm.callbacks = []
    calls = []

    def send(client, request, **kwargs):
        payload = json.loads(request.content)
        calls.append((request, payload))
        path = request.url.path
        if path.endswith("/responses"):
            body = dict(
                id="resp_fixture",
                object="response",
                created_at=1,
                status="completed",
                model="fixture-actual",
                parallel_tool_calls=False,
                output=[
                    dict(
                        id="msg_fixture",
                        type="message",
                        status="completed",
                        role="assistant",
                        content=[dict(type="output_text", text='{"ok":true}', annotations=[])],
                    )
                ],
                usage=dict(input_tokens=10, output_tokens=2, total_tokens=12, cost=0.001),
            )
        elif path.endswith("/decisions"):
            body = dict(
                id="decision_fixture",
                model="fixture-actual",
                answers={"chat": {"type": "noul", "noul": 0.99}},
                usage=dict(input_tokens=10, output_tokens=2, cost=0.001),
            )
        elif path.endswith("/embeddings"):
            body = dict(
                object="list",
                model="fixture-actual",
                data=[dict(object="embedding", index=0, embedding=[0.1, 0.2])],
                usage=dict(prompt_tokens=3, total_tokens=3),
            )
        else:
            body = dict(
                id="chat_fixture",
                object="chat.completion",
                created=1,
                model="fixture-actual",
                choices=[
                    dict(
                        index=0,
                        finish_reason="stop",
                        message=dict(role="assistant", content='{"ok":true}'),
                    )
                ],
                usage=dict(prompt_tokens=10, completion_tokens=2, total_tokens=12, cost=0.001),
            )
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    return litellm, calls


@pytest.mark.parametrize("provider", ["openai", "openrouter"])
def test_chat_and_structured_transformations(transport, provider):
    litellm, calls = transport
    model = "openai/gpt-4o-mini" if provider == "openai" else "openrouter/openrouter/free"
    result = litellm.completion(
        model=model,
        api_key="synthetic-key",
        messages=[{"role": "user", "content": "public fixture"}],
        num_retries=0,
        max_retries=0,
        fallbacks=[],
        max_tokens=64,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "test",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        },
        extra_body={"provider": {"allow_fallbacks": False}} if provider == "openrouter" else {},
    )
    assert len(calls) == 1
    request, payload = calls[0]
    assert request.headers["authorization"] == "Bearer synthetic-key"
    assert request.url.host == ("api.openai.com" if provider == "openai" else "openrouter.ai")
    assert payload["response_format"]["type"] == "json_schema"
    if provider == "openrouter":
        assert payload["model"] == "openrouter/free"
        assert payload["provider"]["allow_fallbacks"] is False
        assert (
            result._hidden_params["additional_headers"]["llm_provider-x-litellm-response-cost"]
            == 0.001
        )
    assert result.usage.prompt_tokens == 10


def test_openai_responses_preserves_stateless_tool_parameters(transport):
    litellm, calls = transport
    result = litellm.responses(
        model="openai/gpt-5-mini",
        api_key="synthetic-key",
        input="public state",
        instructions="Use a function",
        store=False,
        parallel_tool_calls=False,
        tools=[{"type": "function", "name": "read_document", "parameters": {"type": "object"}}],
        tool_choice={"type": "function", "name": "read_document"},
        reasoning={"effort": "low"},
        text={"verbosity": "low"},
        max_output_tokens=64,
        max_retries=0,
        num_retries=0,
    )
    assert len(calls) == 1
    payload = calls[0][1]
    assert payload["store"] is False
    assert payload["parallel_tool_calls"] is False
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["text"] == {"verbosity": "low"}
    assert payload["tool_choice"]["name"] == "read_document"
    assert result.usage.input_tokens == 10


def test_openrouter_native_x_parameters(transport):
    litellm, calls = transport
    result = litellm.responses(
        model="openrouter/x-ai/grok-4.3",
        api_key="synthetic-key",
        input="public fixture",
        store=False,
        max_output_tokens=64,
        max_tool_calls=1,
        max_retries=0,
        num_retries=0,
        tools=[
            {
                "type": "openrouter:web_search",
                "parameters": {
                    "engine": "native",
                    "x_search": {
                        "allowed_x_handles": ["fixture"],
                        "from_date": "2026-11-10",
                        "to_date": "2026-11-11",
                    },
                },
            }
        ],
        extra_body={"provider": {"order": ["xAI"], "allow_fallbacks": False}},
    )
    assert len(calls) == 1
    request, payload = calls[0]
    assert request.url == "https://openrouter.ai/api/v1/responses"
    assert payload["tools"][0]["parameters"]["x_search"]["allowed_x_handles"] == ["fixture"]
    assert payload["max_tool_calls"] == 1
    assert payload["provider"]["allow_fallbacks"] is False
    assert result.usage.cost == 0.001


def test_decisions_state_questions_usage(transport):
    litellm, calls = transport
    result = litellm.systemone(
        model="openrouter/typesafe/jev-1.13",
        api_key="synthetic-key",
        state={"request": "public fixture"},
        questions={"chat": {"type": "noul", "instructions": "Is this chat?"}},
        max_retries=0,
        num_retries=0,
    )
    assert len(calls) == 1
    request, payload = calls[0]
    assert str(request.url) == "https://openrouter.ai/api/alpha/decisions"
    assert payload["state"]["request"] == "public fixture"
    assert result.answers["chat"].noul == 0.99
    assert result.usage.model_extra["cost"] == 0.001


@pytest.mark.parametrize("provider", ["openai", "openrouter"])
def test_embedding_order_and_dimensions(transport, provider):
    litellm, calls = transport
    model = (
        "openai/text-embedding-3-small"
        if provider == "openai"
        else "openrouter/openai/text-embedding-3-small"
    )
    result = litellm.embedding(
        model=model,
        api_key="synthetic-key",
        input=["public fixture"],
        dimensions=2,
        max_retries=0,
        num_retries=0,
    )
    assert len(calls) == 1
    assert calls[0][1]["dimensions"] == 2
    assert result.data[0]["embedding"] == [0.1, 0.2]


def test_actual_sdk_does_not_retry_a_503(transport, monkeypatch):
    litellm, calls = transport

    def fail(client, request, **kwargs):
        calls.append(request)
        return httpx.Response(
            503, json={"error": {"message": "synthetic failure"}}, request=request
        )

    monkeypatch.setattr(httpx.Client, "send", fail)
    with pytest.raises(litellm.ServiceUnavailableError):
        litellm.completion(
            model="openai/gpt-4o-mini",
            api_key="synthetic-key",
            messages=[{"role": "user", "content": "fixture"}],
            max_retries=0,
            num_retries=0,
            fallbacks=[],
        )
    assert len(calls) == 1
