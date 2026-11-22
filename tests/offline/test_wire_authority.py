"""SDK output must preserve reserved framing and the approved request authority."""

import copy
import json

import httpx
import pytest
from test_provider_runtime import alias, payload

from llm_gateway.config.models import ProviderConfig
from llm_gateway.contracts import ChatInput, Message
from llm_gateway.providers.preparation import prepare
from llm_gateway.providers.runtime import (
    ProviderFailure,
    ProviderRuntime,
    checked_wire_parameters,
    initialize,
)

pytestmark = pytest.mark.offline


def setup_boundary(monkeypatch, mutate):
    initialize()
    import litellm

    provider = ProviderConfig(kind="openrouter", credential={"env_var": "SYNTHETIC_KEY"})
    prepared = prepare(
        alias(model="synthetic/model"),
        provider,
        ChatInput(messages=[Message(role="user", content="public")]),
    )
    actual = prepared.body
    actual.update(actual.pop("extra_body"))
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, json=payload("chat"))

    runtime = ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(send),
        environment={"SYNTHETIC_KEY": "synthetic-secret"},
    )

    def sdk(**kwargs):
        body = copy.deepcopy(actual)
        method, url, headers, content = mutate(body, prepared)
        kwargs["client"].client.request(
            method,
            url,
            headers=headers,
            **({"content": content} if content is not None else {"json": body}),
        )

    monkeypatch.setattr(litellm, "completion", sdk)
    return runtime, prepared, provider, calls


@pytest.mark.parametrize(
    "bad",
    [
        "plugins",
        "hosted_tool",
        "routing",
        "storage",
        "message",
        "generation",
        "usage_false",
        "usage_int",
        "ambiguous_caps",
        "output_bool",
        "output_none",
        "output_increase",
        "wrong_cap_kind",
        "framing",
        "model",
        "json",
        "root",
        "method",
        "url",
        "auth",
    ],
)
def test_sdk_cannot_expand_authority_or_spend_reserved_framing(monkeypatch, bad):
    def mutate(body, prepared):
        method, url = "POST", "https://openrouter.ai/api/v1/chat/completions"
        headers, content = {"Authorization": "Bearer synthetic-secret"}, None
        if bad == "plugins":
            body["plugins"] = [{"id": "web"}]
        elif bad == "hosted_tool":
            body["tools"] = [{"type": "openrouter:web_search"}]
        elif bad == "routing":
            body["provider"]["allow_fallbacks"] = True
        elif bad == "storage":
            body["store"] = True
        elif bad == "message":
            body["messages"][0]["content"] = "altered"
        elif bad == "generation":
            body["temperature"] = 0.5
        elif bad.startswith("usage_"):
            body["usage"] = {"include": False if bad == "usage_false" else 1}
        elif bad == "ambiguous_caps":
            body["max_completion_tokens"] = body["max_tokens"]
        elif bad == "output_bool":
            body["max_tokens"] = True
        elif bad == "output_none":
            body["max_tokens"] = None
        elif bad == "output_increase":
            body["max_tokens"] += 1
        elif bad == "wrong_cap_kind":
            body["max_output_tokens"] = body.pop("max_tokens")
        elif bad == "framing":
            body["padding"] = "x" * 300
            size = len(
                json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
            )
            assert prepared.wire_byte_limit < size <= prepared.input_token_bound
        elif bad == "model":
            body["model"] = "ungranted"
        elif bad == "json":
            content = b'{"invalid"'
        elif bad == "root":
            content = b"[]"
        elif bad == "method":
            method = "GET"
        elif bad == "url":
            url = "https://untrusted.invalid/"
        else:
            headers["Authorization"] = "Bearer unrelated"
        return method, url, headers, content

    runtime, prepared, provider, calls = setup_boundary(monkeypatch, mutate)
    with pytest.raises(ProviderFailure) as error:
        runtime.invoke(prepared, provider)
    assert not error.value.dispatched and error.value.evidence is None and calls == []
    assert "synthetic-secret" not in str(error.value)


def test_opaque_sdk_second_request_is_denied_but_first_cost_evidence_survives(monkeypatch):
    runtime, prepared, provider, calls = setup_boundary(
        monkeypatch,
        lambda body, call: (
            "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            {"Authorization": "Bearer synthetic-secret"},
            None,
        ),
    )
    import litellm

    original = litellm.completion

    def retry(**kwargs):
        original(**kwargs)
        original(**kwargs)

    monkeypatch.setattr(litellm, "completion", retry)
    with pytest.raises(ProviderFailure) as error:
        runtime.invoke(prepared, provider)
    assert error.value.code == "opaque_retry_denied" and error.value.dispatched
    assert len(calls) == 1 and error.value.evidence.measured_usd is not None


def test_only_openai_chat_can_use_its_completion_token_alias():
    provider = ProviderConfig(kind="openai")
    prepared = prepare(
        alias(), provider, ChatInput(messages=[Message(role="user", content="public")])
    )
    body = prepared.body
    renamed = copy.deepcopy(body)
    renamed["max_completion_tokens"] = renamed.pop("max_tokens") - 1
    checked_wire_parameters(prepared, renamed, body)
    with pytest.raises(ProviderFailure, match="provider_output_bound_missing"):
        checked_wire_parameters(prepared, renamed | {"max_tokens": 1}, body)


@pytest.mark.parametrize("bad", ["extra", "count", "name", "duplicate", "framing", "model"])
def test_openai_decisions_projection_cannot_add_work_or_borrow_question_reserves(monkeypatch, bad):
    import importlib

    from llm_gateway.contracts.models import DecisionsInput

    initialize()
    module = importlib.import_module("litellm.decisions.call")
    original = module._request_body
    provider = ProviderConfig(kind="openai", credential={"env_var": "SYNTHETIC_KEY"})
    data = DecisionsInput(
        state={"text": "public"},
        questions={
            name: {"type": "noul", "instructions": "public?"} for name in ("first", "second")
        },
    )
    prepared = prepare(alias("decisions"), provider, data)

    def changed(call):
        body = copy.deepcopy(original(call))
        if bad == "extra":
            body["unapproved"] = True
        elif bad == "count":
            body["questions"].append(body["questions"][0] | {"name": "third"})
        elif bad == "name":
            body["questions"][0]["name"] = "other"
        elif bad == "duplicate":
            body["questions"][1]["name"] = body["questions"][0]["name"]
        elif bad == "model":
            body["model"] = "ungranted"
        else:
            body["input"] = "x" * (prepared.wire_byte_limit + 1)
            size = len(
                json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
            )
            assert prepared.wire_byte_limit < size <= prepared.input_token_bound
        return body

    monkeypatch.setattr(module, "_request_body", changed)
    calls = []

    def forbidden(request):
        calls.append(request)
        pytest.fail("Changed Decisions projection reached HTTP")

    runtime = ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(forbidden),
        environment={"SYNTHETIC_KEY": "synthetic-secret"},
    )
    with pytest.raises(ProviderFailure) as error:
        runtime.invoke(prepared, provider)
    assert not error.value.dispatched and calls == [] and error.value.evidence is None
