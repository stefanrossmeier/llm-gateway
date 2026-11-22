"""COST-01/07, MODEL-05/06/11, SEC-10: meaningful public boundary tests."""

import json
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from llm_gateway.contracts import (
    AttemptReceipt,
    CallerLimits,
    ChatInput,
    FunctionCall,
    InferenceRequest,
    RequestContext,
    ResponsesInput,
    TokenUsage,
    Usd,
    add_usd,
)
from llm_gateway.contracts.models import DecisionsInput, Message, XSearchInput
from llm_gateway.service.cli import require_service_python

pytestmark = pytest.mark.offline
money = TypeAdapter(Usd)


@pytest.mark.parametrize(
    "value",
    [
        0.1,
        True,
        0,
        "-0",
        "-1",
        "NaN",
        "Infinity",
        "1e-5",
        "0.0000000000000000001",
        "1000000001",
        Decimal("NaN"),
    ],
)
def test_money_rejects_unsafe_values(value):
    with pytest.raises(ValidationError):
        money.validate_python(value)


def test_money_exact_boundary_and_serialization():
    assert add_usd(Decimal("0.1"), Decimal("0.2")) == Decimal("0.3")
    assert money.dump_json(money.validate_python("0.1000")) == b'"0.1"'
    assert money.validate_python("1000000000") == Decimal("1000000000")
    assert money.validate_python("0.000000000000000001") == Decimal("1e-18")


@pytest.mark.parametrize("field", ["api_key", "api_base", "previous_response_id", "callbacks"])
def test_request_rejects_transport_or_state_overrides(field):
    data = dict(
        request_id="call-1",
        alias="test",
        operation="chat",
        input={"kind": "chat", "messages": [{"role": "user", "content": "hi"}]},
    )
    data[field] = "untrusted"
    with pytest.raises(ValidationError):
        InferenceRequest.model_validate(data)


def test_operation_cannot_mismatch_input():
    with pytest.raises(ValidationError):
        InferenceRequest(
            request_id="one",
            alias="test",
            operation="responses",
            input=ChatInput(messages=[{"role": "user", "content": "hi"}]),
        )


@pytest.mark.parametrize("reference_key", ["$ref", "$dynamicRef"])
def test_schema_network_reference_rejected(reference_key):
    with pytest.raises(ValidationError):
        ChatInput(
            messages=[{"role": "user", "content": "hi"}],
            response_format="json_schema",
            response_schema={reference_key: "http://internal/secret"},
        )


def test_malformed_function_arguments_remain_observable():
    call = FunctionCall(
        call_id="one", name="read_document", arguments="{bad", diagnostic="invalid_json"
    )
    assert call.arguments == "{bad"
    assert call.parsed_arguments is None
    assert ResponsesInput(input="state").store is False
    with pytest.raises(ValidationError):
        ResponsesInput(input="state", store=True)
    with pytest.raises(ValidationError):
        ResponsesInput(input="state", store=0)


def test_function_result_items_and_message_tool_protocol():
    response = ResponsesInput(
        input=[{"type": "function_call_output", "call_id": "one", "output": "public result"}]
    )
    assert response.input[0].call_id == "one"
    with pytest.raises(ValidationError):
        ChatInput(messages=[{"role": "tool", "content": "unlinked result"}])
    request = ChatInput(
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "one", "function": {"name": "read_document", "arguments": "{bad"}}
                ],
            },
            {"role": "tool", "content": "result", "tool_call_id": "one"},
        ]
    )
    assert request.messages[0].tool_calls[0].function.arguments == "{bad"


def test_absent_usage_is_nullable_and_strict():
    assert TokenUsage().input_tokens is None
    assert json.loads(TokenUsage().model_dump_json())["input_tokens"] is None
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=True)


def test_context_and_deadline_bounds():
    with pytest.raises(ValidationError):
        RequestContext(tags={"tag": "x" * 513})
    with pytest.raises(ValidationError):
        CallerLimits(timeout_seconds=3601)
    with pytest.raises(ValidationError):
        CallerLimits(max_usd=0.1)


def test_receipt_cannot_claim_measured_without_price():
    data = dict(
        receipt_id="receipt",
        project_id="test",
        principal_id="owner",
        request_id="call",
        attempt_id="attempt",
        attempt=1,
        config_digest="a" * 64,
        model=dict(
            alias="test",
            provider_ref="router",
            configured_model="openrouter/test",
            upstream_model="test",
        ),
        protected_debit_usd="0.1",
        contingent_exposure_usd="0.1",
    )
    assert AttemptReceipt(**data).measured_usd is None
    with pytest.raises(ValidationError):
        AttemptReceipt(**data, evidence_status="measured")
    with pytest.raises(ValidationError):
        AttemptReceipt(**data, evidence_status="estimated")
    with pytest.raises(ValidationError):
        AttemptReceipt(**data, usage_complete=True)


def test_service_version_guard():
    require_service_python((3, 12))
    with pytest.raises(RuntimeError, match="service_python_required"):
        require_service_python((3, 11))


@pytest.mark.parametrize("identifier", [".", ".."])
def test_reserved_path_ids_cannot_address_a_request_resource(identifier):
    with pytest.raises(ValidationError, match="reserved path"):
        InferenceRequest(
            request_id=identifier,
            alias="test",
            operation="chat",
            input=ChatInput(messages=[{"role": "user", "content": "public"}]),
        )


@pytest.mark.parametrize("resource", ["depth", "nodes"])
def test_decisions_json_resource_limits_apply_before_preparation(resource):
    state = {"items": [0] * 16385}
    if resource == "depth":
        nested = []
        for _ in range(66):
            nested = [nested]
        state = {"nested": nested}
    with pytest.raises(ValidationError, match="depth/node"):
        DecisionsInput(state=state, questions={})


@pytest.mark.parametrize(
    "message",
    [
        {
            "role": "user",
            "content": "public",
            "tool_calls": [{"id": "call", "function": {"name": "read", "arguments": "{}"}}],
        },
        {"role": "assistant", "content": None},
        {"role": "user", "content": None},
        {"role": "assistant", "content": "public", "tool_call_id": "call"},
    ],
)
def test_message_history_cannot_claim_an_invalid_function_role(message):
    with pytest.raises(ValidationError):
        Message.model_validate(message)


@pytest.mark.parametrize("kind", ["chat", "responses"])
def test_schema_output_requires_an_actual_schema(kind):
    with pytest.raises(ValidationError, match="requires response_schema"):
        if kind == "chat":
            ChatInput(
                messages=[{"role": "user", "content": "public"}], response_format="json_schema"
            )
        else:
            ResponsesInput(input="public", response_format="json_schema")


@pytest.mark.parametrize(
    "changes",
    [
        {"from_date": "2026-01-02"},
        {"to_date": "2025-12-31"},
        {"allowed_x_handles": ["SameHandle", "samehandle"]},
    ],
)
def test_x_scope_dates_and_casefolded_handle_identity_are_unambiguous(changes):
    with pytest.raises(ValidationError):
        XSearchInput.model_validate(
            {
                "input": "public",
                "from_date": "2026-01-01",
                "to_date": "2026-01-02",
                "allowed_x_handles": ["public"],
                "response_schema": {"type": "object"},
            }
            | changes
        )


def test_utf8_tag_byte_limit_is_distinct_from_character_limits():
    assert RequestContext(tags={"label": "🧪" * 512}).tags["label"]
    with pytest.raises(ValidationError, match="32 KiB"):
        RequestContext(tags={str(index): "🧪" * 512 for index in range(32)})
