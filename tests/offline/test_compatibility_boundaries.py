"""Malformed SDK-shaped input stays outside admission; projections stay honest."""

import copy

import pytest
from test_api import auth

from llm_gateway.contracts import InferenceResult
from llm_gateway.service.compatibility import dashboard, openai_request, openai_response, usage
from llm_gateway.service.engine import EngineError

pytestmark = pytest.mark.offline
pytest_plugins = ["test_api"]
SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
CHAT = {"model": "summary", "messages": [{"role": "user", "content": "public"}]}


@pytest.mark.parametrize(
    "change",
    [
        {"store": True},
        {"store": None},
        {"max_tokens": 10, "max_completion_tokens": 10},
        {"tools": None},
        {"tools": {}},
        {"tools": [None]},
        {"tools": [{}] * 129},
        {"tools": [{"type": "web_search"}]},
        {"tools": [{"type": "function", "function": None}]},
        {"tools": [{"type": "function", "function": "not-an-object"}]},
        {
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "read", "parameters": SCHEMA, "provider": "caller-choice"},
                }
            ]
        },
        {"tool_choice": {"type": "web_search", "function": {"name": "read"}}},
        {"tool_choice": {"type": "function", "function": None}},
        {"tool_choice": {"type": "function", "function": {"name": "read", "url": "unsafe"}}},
        {"response_format": {"type": "text", "json_schema": {"name": "ignored"}}},
        {
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": SCHEMA, "strict": False},
            }
        },
        {
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": SCHEMA, "description": "unsupported"},
            }
        },
        {"response_format": {"type": "json_schema", "json_schema": None}},
        {"stream": True},
        {"stream": 0},
    ],
)
def test_malformed_nested_chat_fields_return_safe_errors_before_registration(service, change):
    client, engine, _, _ = service
    response = client.post(
        "/v1/chat/completions",
        json=copy.deepcopy(CHAT | change),
        headers=auth(identifier="malformed"),
    )
    assert response.status_code == 422, response.text
    assert set(response.json()) <= {"error", "receipts"}
    assert engine.ledger.report("project-a")["attempts"] == 0
    with engine.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


@pytest.mark.parametrize(
    "text",
    [
        {"format": {"type": "json_schema", "strict": False, "name": "answer", "schema": SCHEMA}},
        {"format": {"type": "text", "schema": SCHEMA}},
        {"format": {"type": "json_object", "name": "ignored"}},
        {"format": {"type": "text", "strict": True}},
        {"format": None},
        {"unsupported": True},
    ],
)
def test_responses_format_cannot_silently_ignore_schema_controls(service, text):
    client, engine, _, _ = service
    response = client.post(
        "/v1/responses",
        json={"model": "summary", "input": "public", "text": text},
        headers=auth(identifier="bad-response"),
    )
    assert response.status_code == 422
    assert engine.ledger.report("project-a")["attempts"] == 0


def test_chat_named_functions_schema_and_both_single_cap_spellings_translate():
    for cap in ("max_tokens", "max_completion_tokens"):
        result = openai_request(
            "chat",
            CHAT
            | {
                cap: 17,
                "tools": [{"type": "function", "function": {"name": "read", "parameters": SCHEMA}}],
                "tool_choice": {"type": "function", "function": {"name": "read"}},
                "parallel_tool_calls": False,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "answer", "schema": SCHEMA, "strict": True},
                },
            },
            "chat",
        )
        assert result.limits.max_output_tokens == 17
        assert result.input.tools[0].name == "read" and result.input.tool_choice.name == "read"
        assert result.input.response_schema == SCHEMA and result.input.schema_name == "answer"


@pytest.mark.parametrize(
    "format",
    [
        {"type": "text"},
        {"type": "json_object"},
        {"type": "json_schema", "name": "answer", "schema": SCHEMA, "strict": True},
    ],
)
def test_responses_reasoning_verbosity_format_and_output_cap_translate(format):
    result = openai_request(
        "responses",
        {
            "model": "summary",
            "input": "public",
            "instructions": "public instruction",
            "store": False,
            "reasoning": {"effort": "low"},
            "text": {"verbosity": "low", "format": format},
            "max_output_tokens": 19,
        },
        "response",
    )
    assert result.input.response_format == format["type"]
    assert result.input.text_verbosity == "low" and result.input.reasoning_effort == "low"
    assert result.limits.max_output_tokens == 19
    assert result.input.response_schema == format.get("schema")


@pytest.mark.parametrize("input", ["public", ["public", "second"]])
def test_embeddings_string_or_batch_preserves_dimensions_and_encoding(input):
    result = openai_request(
        "embeddings",
        {
            "model": "summary",
            "input": input,
            "dimensions": 2,
            "encoding_format": "base64",
        },
        "embedding",
    )
    assert result.input.input == ([input] if isinstance(input, str) else input)
    assert result.input.dimensions == 2 and result.input.encoding_format == "base64"


def result(**changes):
    model = {
        "alias": "summary",
        "provider_ref": "router",
        "configured_model": "configured",
        "upstream_model": "upstream",
        "actual_model": "observed",
        "generation_id": "gen",
    }
    receipt = {
        "receipt_id": "receipt",
        "project_id": "project-a",
        "principal_id": "owner",
        "request_id": "request",
        "attempt_id": "receipt",
        "attempt": 1,
        "config_digest": "0" * 64,
        "model": model,
        "usage": {
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "cache_tokens": 1,
            "reasoning_tokens": 1,
        },
        "evidence_status": "measured",
        "measured_usd": "0.001",
        "protected_debit_usd": "0.001",
        "contingent_exposure_usd": "0",
        "usage_complete": True,
    }
    return InferenceResult.model_validate(
        {
            "request_id": "request",
            "trace_id": "trace",
            "project_id": "project-a",
            "config_digest": "0" * 64,
            "execution_status": "completed",
            "output_status": "valid",
            "content": {"public": True},
            "model": model,
            "receipts": [receipt],
        }
        | changes
    )


def test_projection_preserves_raw_calls_and_partial_usage_without_fabricating_cost():
    value = result(
        content=None,
        function_calls=[
            {
                "call_id": "call",
                "name": "read",
                "arguments": "{ raw invalid argument bytes }",
                "diagnostic": "invalid_json",
            }
        ],
    )
    chat = openai_response(value, "chat")
    assert chat["choices"][0]["message"]["content"] is None
    assert chat["choices"][0]["finish_reason"] == "tool_calls"
    assert chat["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == (
        "{ raw invalid argument bytes }"
    )
    projected = openai_response(value, "responses")
    assert projected["output"][0]["type"] == "function_call"
    assert projected["usage"]["input_tokens_details"] == {"cached_tokens": 1}
    assert projected["usage"]["output_tokens_details"] == {"reasoning_tokens": 1}
    receipt = value.receipts[0].model_copy(
        update={"measured_usd": None, "evidence_status": "unavailable", "usage_complete": False}
    )
    unknown = value.model_copy(update={"receipts": [receipt]})
    assert dashboard(unknown, latency_ms=7)["usage"]["cost_usd"] is None
    assert dashboard(unknown, latency_ms=7)["usage_complete"] is False
    no_usage = receipt.model_copy(
        update={"usage": receipt.usage.model_copy(update={"output_tokens": None})}
    )
    assert usage(value.model_copy(update={"receipts": [no_usage]})) is None


def test_missing_receipts_and_missing_token_splits_remain_missing():
    missing = result(receipts=[], content=None)
    assert usage(missing) is None
    assert openai_response(missing, "embeddings")["usage"] is None
    assert dashboard(missing, latency_ms=0)["usage"]["cost_usd"] is None
    assert openai_response(missing, "responses")["output"] == []
    full = result()
    receipt = full.receipts[0].model_copy(
        update={
            "usage": full.receipts[0].usage.model_copy(
                update={"cache_tokens": None, "reasoning_tokens": None}
            )
        }
    )
    assert usage(full.model_copy(update={"receipts": [receipt]})) == {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
    }


def test_ordered_outputs_are_preserved_and_synthesized_output_uses_safe_identity():
    original = [
        {"type": "function_call", "call_id": "call", "name": "read", "arguments": "{}"},
        {"type": "message", "content": []},
    ]
    assert (
        openai_response(result(provider_metadata={"output_items": original}), "responses")["output"]
        == original
    )
    value = result(
        model={
            "alias": "summary",
            "provider_ref": "router",
            "configured_model": "configured",
            "upstream_model": "upstream",
        }
    )
    response = openai_response(value, "responses")
    assert response["id"] == "gateway_request" and response["model"] == "upstream"
    assert response["output"][0]["content"][0]["text"] == '{"public": true}'
    assert dashboard(value, latency_ms=9)["usage"]["cost_usd"] == "0.001"


def test_nonobject_compatibility_input_is_explicitly_rejected():
    with pytest.raises(EngineError, match="object_body_required"):
        openai_request("chat", [], "request")
