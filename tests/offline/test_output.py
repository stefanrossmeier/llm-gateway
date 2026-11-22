import pytest
from test_provider_runtime import alias

from llm_gateway.contracts import ChatInput, EmbeddingsInput, FunctionTool, Message, ResponsesInput
from llm_gateway.service.output import normalize

pytestmark = pytest.mark.offline


@pytest.mark.parametrize(
    "arguments,status",
    [("{}", "valid"), ("{broken", "invalid_tool_arguments"), ("[]", "invalid_tool_arguments")],
)
def test_function_arguments_are_preserved_without_repair(arguments, status):
    data = ResponsesInput(
        input="public", tools=[FunctionTool(name="read", parameters={"type": "object"})]
    )
    payload = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": "call-fixture",
                "name": "read",
                "arguments": arguments,
            }
        ],
    }
    output = normalize(payload, data, alias("responses"))
    assert output.status == status
    assert output.function_calls[0].arguments == arguments


@pytest.mark.parametrize(
    "vectors,expected",
    [
        ([{"index": 1, "embedding": [0.2, 0.3]}, {"index": 0, "embedding": [0.1, 0.2]}], "valid"),
        ([{"index": 0, "embedding": [0.1, 0.2]}], "invalid_document"),
        (
            [{"index": 0, "embedding": [0.1, 0.2]}, {"index": 0, "embedding": [0.1, 0.2]}],
            "invalid_document",
        ),
        (
            [{"index": 0, "embedding": [True, 0.2]}, {"index": 1, "embedding": [0.1, 0.2]}],
            "invalid_document",
        ),
    ],
)
def test_embedding_order_and_partial_response_validation(vectors, expected):
    output = normalize(
        {"data": vectors}, EmbeddingsInput(input=["a", "b"], dimensions=2), alias("embeddings")
    )
    assert output.status == expected
    if expected == "valid":
        assert [row["index"] for row in output.content] == [0, 1]


@pytest.mark.parametrize("reason,status", [("length", "truncated"), ("content_filter", "refused")])
def test_refusal_and_truncation_are_distinct(reason, status):
    output = normalize(
        {"choices": [{"finish_reason": reason, "message": {"content": "partial"}}]},
        ChatInput(messages=[Message(role="user", content="public")]),
        alias(),
    )
    assert output.status == status
