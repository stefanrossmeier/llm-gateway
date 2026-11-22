"""Untrusted provider output cannot grant execution or fabricate valid evidence."""

import base64
import copy
import struct

import pytest
from test_provider_runtime import alias

from llm_gateway.contracts import ChatInput, EmbeddingsInput, FunctionTool, Message, ResponsesInput
from llm_gateway.contracts.models import DecisionsInput, FunctionChoice
from llm_gateway.service.output import _document, normalize, ordered_items

pytestmark = pytest.mark.offline


def response(*items):
    return {"status": "completed", "output": list(items)}


def call(name="answer", arguments="{}", **changes):
    return {
        "type": "function_call",
        "call_id": "call-one",
        "name": name,
        "arguments": arguments,
    } | changes


@pytest.mark.parametrize(
    "case",
    [
        "malformed_shape",
        "undeclared",
        "none",
        "missing_required",
        "wrong_named",
        "parallel",
        "not_object",
    ],
)
def test_returned_functions_remain_data_and_must_obey_requested_protocol(case):
    data = ResponsesInput(
        input="public",
        tools=[
            FunctionTool(name="answer", parameters={"type": "object"}),
            FunctionTool(name="other", parameters={"type": "object"}),
        ],
    )
    items = [call()]
    if case == "malformed_shape":
        items = [call(name=42)]
    elif case == "undeclared":
        items = [call(name="undeclared")]
    elif case == "none":
        data = data.model_copy(update={"tool_choice": "none"})
    elif case == "missing_required":
        data = data.model_copy(update={"tool_choice": "required"})
        items = []
    elif case == "wrong_named":
        data = data.model_copy(update={"tool_choice": FunctionChoice(name="other")})
    elif case == "parallel":
        items.append(call(call_id="call-two"))
    else:
        items = [call(arguments="[]")]
    result = normalize(response(*items), data, alias("responses"))
    assert result.status == "invalid_tool_arguments"
    assert len(result.function_calls) == len(items)
    if items:
        assert result.function_calls[0].arguments == items[0]["arguments"]


def test_required_declared_functions_with_parallel_grant_are_valid_and_never_executed():
    data = ResponsesInput(
        input="public",
        tools=[FunctionTool(name="answer", parameters={"type": "object"})],
        tool_choice=FunctionChoice(name="answer"),
        parallel_tool_calls=True,
    )
    result = normalize(response(call(), call(call_id="call-two")), data, alias("responses"))
    assert result.status == "valid" and len(result.function_calls) == 2
    assert all(item.parsed_arguments == {} for item in result.function_calls)


@pytest.mark.parametrize(
    "vectors",
    [
        [{"index": 0, "embedding": []}],
        [{"index": 0, "embedding": [float("nan")]}],
        [{"index": 0, "embedding": [float("inf")]}],
        [{"index": 0, "embedding": "invalid~base64"}],
        [{"index": 0, "embedding": base64.b64encode(b"abc").decode()}],
        [{"index": 0, "embedding": ["text"]}],
        [{"index": 0, "embedding": [0.1]}, {"index": 1, "embedding": [0.2, 0.3]}],
    ],
)
def test_embeddings_reject_empty_nonfinite_malformed_or_inconsistent_vectors(vectors):
    data = EmbeddingsInput(input=["public"] * len(vectors))
    assert normalize({"data": vectors}, data, alias("embeddings")).status == "invalid_document"


def test_embedding_base64_is_strict_and_preserved_with_valid_dimensions():
    encoded = base64.b64encode(struct.pack("<ff", 0.25, 0.5)).decode()
    data = EmbeddingsInput(input=["public"], dimensions=2, encoding_format="base64")
    valid = normalize({"data": [{"index": 0, "embedding": encoded}]}, data, alias("embeddings"))
    assert valid.status == "valid" and valid.content[0]["embedding"] == encoded
    # Data after padding must not be silently ignored by the standard decoder.
    invalid = normalize(
        {"data": [{"index": 0, "embedding": encoded + "AAAA"}]}, data, alias("embeddings")
    )
    assert invalid.status == "invalid_document"
    wrong_dimension = normalize(
        {"data": [{"index": 0, "embedding": [0.1]}]}, data, alias("embeddings")
    )
    assert wrong_dimension.status == "invalid_document"


@pytest.mark.parametrize(
    "answers,status",
    [
        (None, "invalid_document"),
        ({"other": {}}, "invalid_document"),
        ({"question": {"type": "refusal"}}, "refused"),
        ({"question": {"type": "noul", "noul": 0.5}}, "valid"),
    ],
)
def test_decisions_require_every_requested_answer_and_preserve_refusal(answers, status):
    data = DecisionsInput(state={}, questions={"question": {"type": "noul"}})
    assert normalize({"answers": answers}, data, alias("decisions")).status == status


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"status": "failed"}, "invalid_document"),
        ({"status": "incomplete", "output": []}, "truncated"),
        (
            response({"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}),
            "refused",
        ),
        ({"choices": []}, "invalid_document"),
        ({"choices": [42]}, "invalid_document"),
        ({"choices": [{"message": {"content": ["invalid"]}}]}, "invalid_document"),
    ],
)
def test_output_protocol_refusal_truncation_and_malformed_text_are_distinct(payload, status):
    data = (
        ResponsesInput(input="public")
        if "status" in payload
        else ChatInput(messages=[Message(role="user", content="public")])
    )
    assert normalize(payload, data, alias(data.kind)).status == status


def test_ordered_response_items_drop_unknown_fields_and_keep_refusals_and_citations():
    source = [
        {"type": "reasoning", "secret_detail": "not-exported"},
        call(extra_secret="not-exported"),
        {
            "type": "message",
            "role": "assistant",
            "extra_secret": "not-exported",
            "content": [
                {"type": "unknown", "text": "not-exported"},
                {"type": "refusal", "refusal": "no"},
                {
                    "type": "output_text",
                    "text": "public",
                    "annotations": [
                        {"type": "unknown", "detail": "not-exported"},
                        {
                            "type": "url_citation",
                            "url": "https://example.org/",
                            "extra_secret": "not-exported",
                        },
                    ],
                },
            ],
        },
    ]
    before = copy.deepcopy(source)
    items = ordered_items({"output": source})
    assert source == before and len(items) == 2
    assert "not-exported" not in str(items)
    assert items[1]["content"][0] == {"type": "refusal", "refusal": "no"}
    assert items[1]["content"][1]["annotations"] == [
        {"type": "url_citation", "url": "https://example.org/"}
    ]


@pytest.mark.parametrize(
    "text,status",
    [
        ("[]", "invalid_document"),
        ("{broken", "invalid_document"),
        ('{"ok": true}', "valid"),
        ('{"value": NaN}', "invalid_document"),
    ],
)
def test_json_object_output_is_validated_without_repair(text, status):
    data = ChatInput(
        messages=[Message(role="user", content="public")], response_format="json_object"
    )
    payload = {"choices": [{"message": {"content": text}}]}
    assert normalize(payload, data, alias()).status == status


def test_parser_resource_failure_becomes_validation_diagnostic(monkeypatch):
    from llm_gateway.service import output

    def exhausted(*args, **kwargs):
        raise MemoryError()

    monkeypatch.setattr(output.json, "loads", exhausted)
    with pytest.raises(ValueError, match="validation_resource_limit"):
        _document("{}", {"type": "object"})
