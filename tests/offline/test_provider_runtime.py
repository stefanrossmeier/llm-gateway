"""MODEL-01–13, COST-01–09, SEC-01–08: actual SDK, owned mocked HTTP."""

import json
from decimal import Decimal

import httpx
import pytest

from llm_gateway.config.models import AliasConfig, ProviderConfig
from llm_gateway.contracts.models import (
    ChatInput,
    DecisionsInput,
    EmbeddingsInput,
    Message,
    ResponsesInput,
    TokenUsage,
)
from llm_gateway.providers.preparation import PreparationError, prepare
from llm_gateway.providers.pricing import estimate_usd, reserve_usd
from llm_gateway.providers.runtime import ProviderFailure, ProviderRuntime, evidence

pytestmark = pytest.mark.offline


def alias(operation="chat", model="fixture", **changes):
    return AliasConfig.model_validate(
        dict(
            provider_ref="provider",
            upstream_model=model,
            operation=operation,
            capabilities={
                "chat": ["plain_text", "json_object", "json_schema", "function_calls"],
                "responses": ["plain_text", "function_calls", "reasoning_effort", "text_verbosity"],
                "embeddings": ["embedding_dimensions"],
                "decisions": [],
            }[operation],
            limits=dict(
                max_input_bytes=32768,
                max_input_tokens=65536,
                max_output_tokens=None if operation in {"embeddings", "decisions"} else 64,
                attempt_timeout_seconds=1,
                request_timeout_seconds=2,
                max_concurrency=1,
            ),
            pricing=dict(
                revision="fixture",
                bound_source="fixture-reviewed",
                input_bound="utf8_bytes_with_framing",
                framing_tokens_per_item=1024,
                input_usd_per_token="0.000001",
                output_usd_per_token="0.000002",
            ),
        )
        | changes
    )


def payload(operation):
    usage = dict(input_tokens=3, output_tokens=2, total_tokens=5, cost=0.001)
    if operation == "responses":
        return dict(
            id="resp_fixture",
            object="response",
            created_at=1,
            status="completed",
            model="fixture",
            parallel_tool_calls=False,
            output=[],
            usage=usage,
        )
    if operation == "decisions":
        return dict(
            id="dec_fixture",
            model="fixture",
            answers={"ok": {"type": "noul", "noul": 0.9}},
            usage=usage,
        )
    if operation == "embeddings":
        return dict(
            object="list",
            model="fixture",
            data=[dict(object="embedding", index=0, embedding=[0.1, 0.2])],
            usage=dict(prompt_tokens=3),
        )
    return dict(
        id="chat_fixture",
        object="chat.completion",
        created=1,
        model="fixture",
        choices=[dict(index=0, finish_reason="stop", message=dict(role="assistant", content="ok"))],
        usage=dict(prompt_tokens=3, completion_tokens=2, cost=0.001),
    )


@pytest.mark.parametrize("kind", ["openai", "openrouter"])
@pytest.mark.parametrize("operation", ["chat", "responses", "embeddings", "decisions"])
def test_owned_transport_all_operations(kind, operation):
    provider = ProviderConfig(kind=kind, credential={"env_var": "FIXTURE_KEY"})
    data = {
        "chat": ChatInput(messages=[Message(role="user", content="public")]),
        "responses": ResponsesInput(input="public"),
        "embeddings": EmbeddingsInput(input=["public"], dimensions=2),
        "decisions": DecisionsInput(
            state={"x": "public"}, questions={"ok": {"type": "noul", "instructions": "ok?"}}
        ),
    }[operation]
    profile = alias(
        operation,
        ("text-embedding-3-small" if operation == "embeddings" else "fixture")
        if kind == "openai"
        else "fixture/model",
    )
    calls = []

    def send(request):
        calls.append(request)
        body = payload(operation)
        if operation == "decisions" and kind == "openai":
            body["answers"] = [{"type": "predicate", "name": "ok", "probability": 0.9}]
        return httpx.Response(200, json=body)

    runtime = ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(send),
        environment={"FIXTURE_KEY": "fixture-secret"},
    )
    result = runtime.invoke(prepare(profile, provider, data), provider)
    assert len(calls) == 1
    assert calls[0].headers["Authorization"] == "Bearer fixture-secret"
    assert calls[0].url.host == ("api.openai.com" if kind == "openai" else "openrouter.ai")
    assert result.usage.input_tokens == 3
    if operation == "embeddings":
        assert result.usage.output_tokens is None
        assert result.measured_usd is None
    else:
        assert result.measured_usd == Decimal("0.001")


@pytest.mark.parametrize("status", [302, 429, 503])
def test_no_redirect_or_retry(status):
    provider = ProviderConfig(kind="openrouter", credential={"env_var": "FIXTURE_KEY"})
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://evil.invalid"},
            json={"error": {"message": "fixture-secret"}},
        )

    runtime = ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(send),
        environment={"FIXTURE_KEY": "fixture-secret"},
    )
    with pytest.raises(ProviderFailure) as caught:
        runtime.invoke(
            prepare(alias(), provider, ChatInput(messages=[Message(role="user", content="hi")])),
            provider,
        )
    assert len(calls) == 1
    assert caught.value.dispatched
    assert "fixture-secret" not in str(caught.value)
    assert "fixture-secret" not in json.dumps(caught.value.evidence.payload)


def test_raw_usage_absence_is_not_sdk_default_zero_and_echo_is_redacted():
    result = evidence({"model": "echo-secret", "usage": {}, "choices": ["echo-secret"]}, "secret")
    assert result.usage == TokenUsage()
    assert result.measured_usd is None
    assert result.actual_model is None
    for invalid in ["NaN", "-1", None, True]:
        assert evidence({"usage": {"cost": invalid}}, "secret").billing_anomaly


def test_local_pricing_bound_and_unknown_estimate():
    profile = alias()
    assert reserve_usd(profile, 100, 20) == Decimal("0.000140")
    assert estimate_usd(profile, TokenUsage(input_tokens=3)) is None
    assert estimate_usd(profile, TokenUsage(input_tokens=3, output_tokens=2)) == Decimal("0.000007")


def test_preparation_rejects_ungranted_schema_before_transport():
    profile = alias(capabilities=["plain_text"])
    with pytest.raises(PreparationError, match="capability"):
        prepare(
            profile,
            ProviderConfig(kind="openai"),
            ChatInput(messages=[Message(role="user", content="hi")], response_format="json_object"),
        )


def test_free_translation_and_strict_schema_use_actual_transport():
    provider = ProviderConfig(kind="openrouter", credential={"env_var": "FIXTURE_KEY"})
    profile = alias(model="openrouter/free")
    calls = []

    def send(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=payload("chat"))

    data = ChatInput(
        messages=[Message(role="user", content="hi")],
        response_format="json_schema",
        response_schema={"type": "object", "additionalProperties": False},
    )
    runtime = ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(send),
        environment={"FIXTURE_KEY": "synthetic-key"},
    )
    runtime.invoke(prepare(profile, provider, data), provider)
    assert calls[0]["model"] == "openrouter/free"
    assert calls[0]["response_format"]["json_schema"]["strict"] is True
    assert calls[0]["provider"] == {"allow_fallbacks": False, "require_parameters": True}


@pytest.mark.parametrize("choice", ["required", {"type": "function", "name": "record_check"}])
def test_free_function_contract_through_actual_pinned_adapter(choice):
    """Probe the live-failed shape without another provider request or charge."""
    provider = ProviderConfig(kind="openrouter", credential={"env_var": "FIXTURE_KEY"})
    calls = []

    def send(request):
        calls.append(json.loads(request.content))
        body = payload("chat")
        body["choices"][0].update(
            finish_reason="tool_calls",
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-public-fixture",
                        "type": "function",
                        "function": {"name": "record_check", "arguments": '{"ok":true}'},
                    }
                ],
            },
        )
        return httpx.Response(200, json=body)

    data = ChatInput.model_validate(
        {
            "messages": [{"role": "user", "content": "Call record_check."}],
            "tools": [
                {
                    "type": "function",
                    "name": "record_check",
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                }
            ],
            "tool_choice": choice,
        }
    )
    result = ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(send),
        environment={"FIXTURE_KEY": "synthetic-key"},
    ).invoke(prepare(alias(model="openrouter/free"), provider, data), provider)
    assert len(calls) == 1
    expected = (
        choice
        if isinstance(choice, str)
        else {
            "type": "function",
            "function": {"name": "record_check"},
        }
    )
    assert calls[0]["tool_choice"] == expected
    assert calls[0]["tools"][0]["function"]["strict"] is True
    assert calls[0]["parallel_tool_calls"] is False
    assert calls[0]["provider"] == {"allow_fallbacks": False, "require_parameters": True}
    assert result.payload["choices"][0]["message"]["tool_calls"][0]["function"] == {
        "name": "record_check",
        "arguments": '{"ok":true}',
    }


def test_native_x_bounds_and_actual_parameters():
    from llm_gateway.config.models import XSearchPolicy
    from llm_gateway.contracts.models import XSearchInput

    base = alias("responses", "fixture/model").model_dump(mode="json")
    base.update(
        operation="x_search",
        capabilities=["native_x_search", "json_schema"],
        operation_policy_ref="search",
    )
    base["limits"]["max_tool_calls"] = 1
    profile = AliasConfig.model_validate(base)
    policy = XSearchPolicy(
        allowed_x_handles=["fixture"],
        max_handles=1,
        max_utc_days=2,
        max_posts=6,
        max_post_characters=4000,
        provider_order=["xAI"],
        allowed_domains=["x.com", "twitter.com"],
    )
    data = XSearchInput(
        input="public fixture",
        allowed_x_handles=["fixture"],
        from_date="2026-11-10",
        to_date="2026-11-11",
        response_schema={"type": "object", "additionalProperties": False},
    )
    provider = ProviderConfig(kind="openrouter", credential={"env_var": "FIXTURE_KEY"})
    calls = []

    def send(request):
        calls.append(json.loads(request.content))
        body = payload("responses")
        body["output"] = [
            {
                "id": "ws-fixture",
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search", "query": "from:fixture"},
            }
        ]
        return httpx.Response(200, json=body)

    runtime = ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(send),
        environment={"FIXTURE_KEY": "synthetic-key"},
    )
    result = runtime.invoke(prepare(profile, provider, data, operation_policy=policy), provider)
    assert result.payload["output"][0]["action"]["query"] == "from:fixture"
    assert calls[0]["max_tool_calls"] == 1
    assert calls[0]["provider"]["order"] == ["xAI"]
    assert calls[0]["tools"][0]["parameters"]["x_search"]["allowed_x_handles"] == ["fixture"]
    assert "from:fixture" in calls[0]["input"]
    with pytest.raises(PreparationError, match="scope"):
        prepare(
            profile,
            provider,
            data.model_copy(update={"allowed_x_handles": ["another"]}),
            operation_policy=policy,
        )


def test_decisions_paid_bound_is_rejected_and_missing_detail_objects_are_safe():
    from llm_gateway.providers.pricing import PricingError

    with pytest.raises(PricingError, match="decision_output_bound_unavailable"):
        reserve_usd(alias("decisions"), 100, 64)
    result = evidence(
        {"usage": {"input_tokens_details": "malformed", "output_tokens_details": []}}, "secret"
    )
    assert result.usage.cache_tokens is None
    assert result.usage.reasoning_tokens is None


def test_exact_free_router_required_function_reaches_one_owned_mock_http_request():
    """Separate pinned translation support from real free-provider availability."""
    provider = ProviderConfig(kind="openrouter", credential={"env_var": "SYNTHETIC_KEY"})
    profile = alias(model="openrouter/free")
    data = ChatInput(
        messages=[Message(role="user", content="Call record_check with ok true.")],
        tools=[
            {
                "type": "function",
                "name": "record_check",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            }
        ],
        tool_choice="required",
        parallel_tool_calls=False,
    )
    calls = []

    def send(request):
        calls.append(json.loads(request.content))
        assert request.url.host == "openrouter.ai"
        response = payload("chat")
        response.update(model="synthetic/model:free")
        response["usage"]["cost"] = 0
        response["choices"][0].update(
            finish_reason="tool_calls",
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "synthetic-call",
                        "type": "function",
                        "function": {
                            "name": "record_check",
                            "arguments": '{"ok":true}',
                        },
                    }
                ],
            },
        )
        return httpx.Response(200, json=response)

    result = ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(send),
        environment={"SYNTHETIC_KEY": "synthetic-never-real"},
    ).invoke(prepare(profile, provider, data), provider)
    assert len(calls) == 1
    assert calls[0]["model"] == "openrouter/free"
    assert calls[0]["tool_choice"] == "required"
    assert calls[0]["parallel_tool_calls"] is False
    assert calls[0]["tools"][0]["function"]["strict"] is True
    assert result.actual_model == "synthetic/model:free"
    assert result.measured_usd == Decimal("0")
    assert (
        result.payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        == '{"ok":true}'
    )
