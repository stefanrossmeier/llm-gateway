"""Actual pinned adapters must fit closed wire authority and reserved byte limits."""

import base64
import json
import struct

import httpx
import pytest
from test_provider_runtime import alias, payload

from llm_gateway.config.models import ProviderConfig
from llm_gateway.contracts.models import (
    ChatInput,
    DecisionsInput,
    EmbeddingsInput,
    FunctionChoice,
    FunctionTool,
    Message,
    ResponsesInput,
)
from llm_gateway.providers.preparation import prepare
from llm_gateway.providers.runtime import ProviderRuntime

pytestmark = pytest.mark.offline
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


@pytest.mark.parametrize("kind", ["openai", "openrouter"])
@pytest.mark.parametrize(
    "case",
    ["chat_reasoning", "chat_tools", "responses_rich", "decisions_multi", "embeddings_base64"],
)
def test_actual_pin_preserves_rich_request_shapes_and_wire_reserve(kind, case):
    operation = {
        "chat_reasoning": "chat",
        "chat_tools": "chat",
        "responses_rich": "responses",
        "decisions_multi": "decisions",
        "embeddings_base64": "embeddings",
    }[case]
    selected = alias(operation, "synthetic/model" if kind == "openrouter" else "fixture")
    tool = FunctionTool(name="answer", parameters=SCHEMA)
    if case == "chat_reasoning":
        selected = alias(
            "chat",
            "gpt-5-mini" if kind == "openai" else "openai/gpt-5-mini",
            capabilities=["plain_text", "reasoning_effort"],
            generation={"reasoning_effort": "low"},
        )
        data = ChatInput(messages=[Message(role="user", content="public")])
    elif case == "chat_tools":
        data = ChatInput(
            messages=[Message(role="user", content="public")],
            tools=[tool],
            tool_choice=FunctionChoice(name="answer"),
        )
    elif case == "responses_rich":
        selected = alias(
            "responses",
            "synthetic/model" if kind == "openrouter" else "fixture",
            capabilities=[
                "plain_text",
                "function_calls",
                "json_schema",
                "reasoning_effort",
                "text_verbosity",
            ],
        )
        data = ResponsesInput.model_validate(
            {
                "input": [
                    {
                        "type": "function_call",
                        "name": "answer",
                        "call_id": "old-call",
                        "arguments": '{"ok":true}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "old-call",
                        "output": "public-result",
                    },
                    {"role": "user", "content": "public"},
                ],
                "instructions": "Public instructions; do not execute returned functions.",
                "tools": [tool],
                "tool_choice": {"type": "function", "name": "answer"},
                "reasoning_effort": "low",
                "text_verbosity": "low",
                "response_schema": SCHEMA,
            }
        )
    elif case == "decisions_multi":
        data = DecisionsInput(
            state={"summary": "öffentliche 🐙資料" * 10, "context": {"numbers": [1, 2, 3]}},
            questions={
                name: {"type": "noul", "instructions": "public?"} for name in ["first", "second"]
            },
        )
    else:
        selected = alias(
            "embeddings",
            "text-embedding-3-small" if kind == "openai" else "openai/text-embedding-3-small",
        )
        data = EmbeddingsInput(input=["public"], dimensions=2, encoding_format="base64")
    provider = ProviderConfig(kind=kind, credential={"env_var": "SYNTHETIC_KEY"})
    prepared = prepare(selected, provider, data)
    calls = []

    def send(request):
        body = json.loads(request.content)
        calls.append(body)
        size = len(
            json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        )
        assert size <= prepared.wire_byte_limit <= prepared.input_token_bound
        items, multiplier = 1, 1
        if operation == "chat":
            items = len(data.messages)
        elif operation == "responses":
            items = (max(1, len(data.input)) if isinstance(data.input, list) else 1) + bool(
                data.instructions
            )
        elif operation == "decisions":
            items = multiplier = len(data.questions)
        elif operation == "embeddings":
            items = len(data.input)
        # Actual transformed bytes must leave the model framing reserve intact,
        # including the conservative per-question Decisions multiplication.
        assert (
            size + selected.pricing.framing_tokens_per_item * items
        ) * multiplier <= prepared.input_token_bound
        answer = payload(operation)
        if operation == "decisions":
            answer["answers"] = (
                [{"type": "predicate", "name": name, "probability": 0.7} for name in data.questions]
                if kind == "openai"
                else {name: {"type": "noul", "noul": 0.7} for name in data.questions}
            )
        elif operation == "embeddings":
            answer["data"][0]["embedding"] = base64.b64encode(
                struct.pack("<ff", 0.25, 0.5)
            ).decode()
        return httpx.Response(200, json=answer)

    runtime = ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(send),
        environment={"SYNTHETIC_KEY": "synthetic"},
    )
    observed = runtime.invoke(prepared, provider)
    assert len(calls) == 1 and observed.usage.input_tokens is not None
    if operation == "decisions":
        assert set(observed.payload["answers"]) == set(data.questions)
