"""MODEL/COST typed preparation must enforce capabilities and caller ceilings."""

import copy
from datetime import date

import pytest
from test_paid_proposal import project
from test_provider_runtime import alias

from llm_gateway.config.models import DecisionsPolicy, EmbeddingsPolicy, ProviderConfig
from llm_gateway.contracts import CallerLimits
from llm_gateway.contracts.models import (
    ChatInput,
    DecisionsInput,
    EmbeddingsInput,
    FunctionChoice,
    FunctionTool,
    Message,
    MessageFunction,
    MessageToolCall,
    ResponsesInput,
    TextPart,
    XSearchInput,
)
from llm_gateway.providers.preparation import PreparationError, checked_schema, prepare

pytestmark = pytest.mark.offline
TOOL = FunctionTool(name="answer", parameters={"type": "object"})


@pytest.mark.parametrize("operation", ["chat", "responses"])
@pytest.mark.parametrize(
    "bad", ["parallel", "duplicate", "missing_named", "required_empty", "function_capability"]
)
def test_tool_authority_is_checked_before_constructing_provider_request(operation, bad):
    selected = alias(operation)
    values = {"tools": [TOOL]}
    if bad == "parallel":
        values["parallel_tool_calls"] = True
    elif bad == "duplicate":
        values["tools"] = [TOOL, TOOL]
    elif bad == "missing_named":
        values["tool_choice"] = FunctionChoice(name="unknown")
    elif bad == "required_empty":
        values.update(tools=[], tool_choice="required")
    else:
        selected = selected.model_copy(update={"capabilities": ["plain_text"]})
    data = (
        ChatInput(messages=[Message(role="user", content="public")], **values)
        if operation == "chat"
        else ResponsesInput(input="public", **values)
    )
    with pytest.raises(PreparationError):
        prepare(selected, ProviderConfig(kind="openai"), data)


@pytest.mark.parametrize(
    "limit", [{"timeout_seconds": 3}, {"max_attempts": 2}, {"max_output_tokens": 65}]
)
def test_caller_cannot_expand_a_profile_ceiling(limit):
    with pytest.raises(PreparationError, match="caller_limit_expansion"):
        prepare(
            alias(),
            ProviderConfig(kind="openai"),
            ChatInput(messages=[Message(role="user", content="public")]),
            caller_limits=CallerLimits(**limit),
        )


@pytest.mark.parametrize("operation", ["chat", "responses"])
@pytest.mark.parametrize("format", ["text", "json_object", "json_schema"])
def test_every_effective_output_format_requires_its_capability(operation, format):
    selected = alias(operation).model_copy(update={"capabilities": []})
    values = {
        "response_format": format,
        "response_schema": {"type": "object"} if format == "json_schema" else None,
    }
    data = (
        ChatInput(messages=[Message(role="user", content="public")], **values)
        if operation == "chat"
        else ResponsesInput(input="public", **values)
    )
    with pytest.raises(PreparationError, match="required_capability_unavailable"):
        prepare(selected, ProviderConfig(kind="openai"), data)


@pytest.mark.parametrize(
    "capability,field",
    [("reasoning_effort", "reasoning_effort"), ("text_verbosity", "text_verbosity")],
)
def test_responses_request_options_cannot_add_ungranted_capabilities(capability, field):
    selected = alias("responses").model_copy(update={"capabilities": ["plain_text"]})
    with pytest.raises(PreparationError, match="required_capability_unavailable"):
        prepare(
            selected,
            ProviderConfig(kind="openai"),
            ResponsesInput(input="public", **{field: "low"}),
        )


@pytest.mark.parametrize(
    "schema,code",
    [
        ({"$schema": "https://json-schema.org/draft-07/schema"}, "schema_dialect_unsupported"),
        ({"type": "invalid-type"}, "response_schema_invalid"),
        ({"$ref": "#/missing"}, "response_schema_reference_invalid"),
        ({"$dynamicRef": "https://untrusted.invalid/schema"}, "response_schema_reference_invalid"),
    ],
)
def test_invalid_or_unresolvable_schema_cannot_trigger_remote_resolution(schema, code):
    with pytest.raises(PreparationError, match=code):
        checked_schema(schema)


def test_local_boolean_and_dynamic_schema_resources_are_valid():
    schema = {
        "$defs": {"yes": True, "thing": {"$dynamicAnchor": "thing", "type": "object"}},
        "$ref": "#/$defs/yes",
        "properties": {"nested": {"$dynamicRef": "#thing"}},
    }
    assert checked_schema(schema) == schema


def test_responses_complete_stateless_function_history_is_preserved():
    invocation = MessageToolCall(
        id="call-one", function=MessageFunction(name="answer", arguments='{"n":1}')
    )
    data = ResponsesInput(
        input=[
            Message(role="assistant", content=None, tool_calls=[invocation]),
            Message(
                role="tool",
                tool_call_id="call-one",
                content=[TextPart(type="text", text="public-result")],
            ),
            Message(role="user", content=[TextPart(type="input_text", text="continue")]),
            {"type": "function_call_output", "call_id": "other", "output": "second-result"},
        ],
        tools=[TOOL],
        tool_choice=FunctionChoice(name="answer"),
    )
    prepared = prepare(alias("responses"), ProviderConfig(kind="openai"), data)
    items = prepared.body["input"]
    assert items[0] == {
        "type": "function_call",
        "call_id": "call-one",
        "name": "answer",
        "arguments": '{"n":1}',
    }
    assert items[1]["type"] == "function_call_output" and "public-result" in items[1]["output"]
    assert items[2]["content"] == [{"type": "input_text", "text": "continue"}]
    assert items[3]["output"] == "second-result"
    assert prepared.body["store"] is False
    assert prepared.body["tool_choice"] == {"type": "function", "name": "answer"}


def test_chat_parts_temperature_reasoning_and_smaller_caller_cap_are_explicit():
    selected = alias()
    selected = selected.model_copy(
        update={
            "generation": selected.generation.model_copy(
                update={"temperature": 0.2, "reasoning_effort": "low"}
            ),
            "limits": selected.limits.model_copy(update={"max_reasoning_tokens": 64}),
        }
    )
    data = ChatInput(
        messages=[
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    MessageToolCall(
                        id="call-one", function=MessageFunction(name="answer", arguments="{}")
                    )
                ],
            ),
            Message(role="user", content=[TextPart(type="input_text", text="public")]),
        ]
    )
    body = prepare(
        selected,
        ProviderConfig(kind="openai"),
        data,
        caller_limits=CallerLimits(max_output_tokens=32),
    ).body
    assert body["temperature"] == 0.2 and body["reasoning_effort"] == "low"
    assert body["messages"][0]["content"] is None
    assert body["messages"][1]["content"][0]["type"] == "text"
    assert body["max_tokens"] == 32


@pytest.mark.parametrize(
    "case",
    [
        "route",
        "disabled",
        "input_bytes",
        "transformed_bytes",
        "input_tokens",
        "reasoning_cap",
        "embedding_output_cap",
    ],
)
def test_preparation_enforces_both_original_and_transformed_bounds(case):
    selected, provider = alias(), ProviderConfig(kind="openai")
    data = ChatInput(messages=[Message(role="user", content="public")])
    limits = None
    if case == "route":
        data = EmbeddingsInput(input=["public"])
    elif case == "disabled":
        provider = provider.model_copy(update={"enabled": False})
    elif case in {"input_bytes", "transformed_bytes"}:
        if case == "transformed_bytes":
            data = ChatInput(messages=[Message(role="user", content="é" * 100)])
        size = 1 if case == "input_bytes" else len(data.model_dump_json().encode())
        selected = selected.model_copy(
            update={"limits": selected.limits.model_copy(update={"max_input_bytes": size})}
        )
    elif case == "input_tokens":
        selected = selected.model_copy(
            update={"limits": selected.limits.model_copy(update={"max_input_tokens": 1})}
        )
    elif case == "reasoning_cap":
        selected = selected.model_copy(
            update={"limits": selected.limits.model_copy(update={"max_reasoning_tokens": 1})}
        )
    else:
        selected, data = alias("embeddings"), EmbeddingsInput(input=["public"])
        limits = CallerLimits(max_output_tokens=1)
    with pytest.raises(PreparationError):
        prepare(selected, provider, data, caller_limits=limits)


def test_policy_limits_on_decision_questions_and_embedding_dimensions():
    provider = ProviderConfig(kind="openai")
    with pytest.raises(PreparationError, match="decision_question_limit"):
        prepare(
            alias("decisions"),
            provider,
            DecisionsInput(state={}, questions={"one": {}, "two": {}}),
            operation_policy=DecisionsPolicy(kind="decisions", max_questions=1),
        )
    selected = alias("embeddings")
    policy = EmbeddingsPolicy(kind="embeddings", dimensions=2, max_batch_items=1)
    for data in (
        EmbeddingsInput(input=["one", "two"]),
        EmbeddingsInput(input=["one"], dimensions=3),
    ):
        with pytest.raises(PreparationError, match="embedding_batch_or_dimension_mismatch"):
            prepare(selected, provider, data, operation_policy=policy)
    with pytest.raises(PreparationError, match="required_capability_unavailable"):
        prepare(
            selected.model_copy(update={"capabilities": []}),
            provider,
            EmbeddingsInput(input=["one"], dimensions=2),
            operation_policy=policy,
        )
    assert (
        prepare(selected, provider, EmbeddingsInput(input=["one"]), operation_policy=policy).body[
            "dimensions"
        ]
        == 2
    )


@pytest.mark.parametrize("bad", ["missing_policy", "wrong_provider", "handle", "days"])
def test_native_search_preparation_preserves_scope_even_while_live_billing_is_blocked(
    tmp_path, bad
):
    config = project(tmp_path).config
    selected, policy = config.aliases["search"], config.operation_policies["scope"]
    values = {
        "kind": "x_search",
        "input": "public",
        "allowed_x_handles": ["public_example"],
        "from_date": date(2026, 11, 10),
        "to_date": date(2026, 11, 11),
        "response_schema": {"type": "object"},
    }
    provider = ProviderConfig(kind="openrouter")
    if bad == "missing_policy":
        policy = None
    elif bad == "wrong_provider":
        provider = ProviderConfig(kind="openai")
    elif bad == "handle":
        values["allowed_x_handles"] = ["other"]
    else:
        values["from_date"] = date(2026, 10, 1)
    with pytest.raises(PreparationError):
        prepare(
            selected,
            provider,
            XSearchInput.model_validate(copy.deepcopy(values)),
            operation_policy=policy,
        )
