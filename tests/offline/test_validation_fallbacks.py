"""Failure of an isolated validator must preserve safe, nonexecutable diagnostics."""

import threading

import pytest
from test_provider_runtime import alias

from llm_gateway.config.models import ProviderConfig
from llm_gateway.contracts import CallerLimits, ChatInput, Message, ResponsesInput
from llm_gateway.isolation.process import WorkerFailure
from llm_gateway.providers.preparation import PreparationError
from llm_gateway.service.validation import ValidationProcess, _decode, _failed_output

pytestmark = pytest.mark.offline


@pytest.mark.parametrize("kind", ["responses", "chat", "malformed", "surrogate"])
def test_fallback_retains_only_well_formed_function_strings(kind):
    if kind == "chat":
        data = ChatInput(messages=[Message(role="user", content="public")])
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"id": "call-one", "function": {"name": "answer", "arguments": "{}"}}
                        ]
                    }
                }
            ]
        }
    else:
        data = ResponsesInput(input="public")
        payload = {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-one",
                    "name": "answer",
                    "arguments": "{}",
                }
            ]
        }
        if kind == "malformed":
            payload["output"][0]["arguments"] = 42
        elif kind == "surrogate":
            payload["output"][0]["arguments"] = "\ud800"
    result = _failed_output(payload, data)
    if kind in {"malformed", "surrogate"}:
        assert result.status == "invalid_document" and result.function_calls == []
    else:
        assert result.status == "invalid_tool_arguments"
        assert result.function_calls[0].arguments == "{}"
        assert result.function_calls[0].diagnostic == "validation_resource_limit"
    assert result.metadata["validation_diagnostic"] == "validation_deadline_or_resource_limit"


@pytest.mark.parametrize("fault", ["os", "worker"])
def test_prepare_validation_failure_has_safe_diagnostic_before_provider(
    tmp_path, monkeypatch, fault
):
    process = ValidationProcess(tmp_path)

    def fail(*args, **kwargs):
        if fault == "os":
            raise OSError("private-canary")
        raise WorkerFailure("private-canary", submitted=False)

    monkeypatch.setattr(process, "_run", fail)
    with pytest.raises(PreparationError) as error:
        process.prepare(
            alias(),
            ProviderConfig(kind="openai"),
            ChatInput(messages=[Message(role="user", content="public")]),
            operation_policy=None,
            caller_limits=CallerLimits(),
            remaining=1,
            cancel=threading.Event(),
        )
    assert "private-canary" not in str(error.value)
    assert str(error.value) in {
        "schema_validation_unavailable",
        "schema_validation_deadline_or_resource_limit",
    }


def test_bad_worker_decode_is_rejected_and_empty_non_tool_fallback_is_safe():
    with pytest.raises(ValueError, match="validation_failed"):
        _decode({"error": "private-canary"})
    from llm_gateway.contracts import EmbeddingsInput

    result = _failed_output({}, EmbeddingsInput(input=["public"]))
    assert result.status == "invalid_document" and result.function_calls == []
    result = _failed_output(
        {"choices": [42]}, ChatInput(messages=[Message(role="user", content="public")])
    )
    assert result.status == "invalid_document" and result.function_calls == []
