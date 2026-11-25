"""Key-free, deadline-bounded schema preparation and output validation."""

from pathlib import Path

from llm_gateway.contracts import FunctionCall
from llm_gateway.isolation.process import WorkerFailure, run_worker
from llm_gateway.providers.preparation import PreparationError, PreparedCall
from llm_gateway.providers.process import worker_environment

from .output import Output, ordered_items


def _decode(response):
    if "error" in response:
        raise ValueError("validation_failed")
    output = response["output"]
    output["function_calls"] = [
        FunctionCall.model_validate(call) for call in output["function_calls"]
    ]
    return Output(**output)


def _failed_output(payload, data):
    metadata = {"validation_diagnostic": "validation_deadline_or_resource_limit"}
    functions = []
    try:
        if data.kind == "responses":
            metadata["output_items"] = ordered_items(payload)
            calls = [
                item for item in payload.get("output", []) if item.get("type") == "function_call"
            ]
        elif data.kind == "chat":
            calls = [
                {"call_id": c.get("id"), **c.get("function", {})}
                for c in payload.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
            ]
        else:
            calls = []
        for call in calls:
            values = {
                "call_id": call.get("call_id"),
                "name": call.get("name"),
                "arguments": call.get("arguments"),
            }
            if all(isinstance(v, str) for v in values.values()):
                for value in values.values():
                    value.encode("utf-8")
                functions.append(FunctionCall(**values, diagnostic="validation_resource_limit"))
    except Exception:
        functions = []
    return Output(
        "invalid_tool_arguments" if functions else "invalid_document",
        function_calls=functions,
        metadata=metadata,
    )


class ValidationProcess:
    def __init__(self, root):
        self.root = root

    def _run(self, message, *, remaining, cancel):
        return run_worker(
            Path(__file__).with_name("validation_worker.py"),
            message,
            state_root=self.root,
            environment=lambda private: worker_environment(
                None, environment={}, private_home=private
            ),
            timeout=min(2.0, remaining),
            cancel=cancel,
        )

    def prepare(self, alias, provider, data, *, operation_policy, caller_limits, remaining, cancel):
        try:
            response = self._run(
                {
                    "action": "prepare",
                    "alias": alias.model_dump(mode="json"),
                    "provider": provider.model_dump(mode="json", exclude={"credential"}),
                    "input": data.model_dump(mode="json"),
                    "policy": operation_policy.model_dump(mode="json")
                    if operation_policy
                    else None,
                    "limits": caller_limits.model_dump(mode="json"),
                },
                remaining=remaining,
                cancel=cancel,
            )
            if "error" in response:
                raise PreparationError(response["error"])
            return PreparedCall(**response["prepared"])
        except WorkerFailure as error:
            raise PreparationError("schema_validation_deadline_or_resource_limit") from error
        except OSError:
            raise PreparationError("schema_validation_unavailable") from None

    def normalize(
        self,
        payload,
        data,
        alias,
        *,
        operation_policy,
        remaining,
        cancel,
        quota_period=None,
        request_identity=None,
    ):
        try:
            response = self._run(
                {
                    "action": "normalize",
                    "alias": alias.model_dump(mode="json"),
                    "input": data.model_dump(mode="json"),
                    "policy": operation_policy.model_dump(mode="json")
                    if operation_policy
                    else None,
                    "payload": {
                        key: payload[key]
                        for key in ("status", "choices", "output", "answers", "data", "citations")
                        if key in payload
                    },
                    "quota_period": quota_period,
                    "request_identity": request_identity,
                },
                remaining=remaining,
                cancel=cancel,
            )
            return _decode(response)
        except (WorkerFailure, OSError, ValueError, TypeError, KeyError):
            return _failed_output(payload, data)

    def cached_x(self, cached, data, alias, policy, *, remaining, cancel):
        try:
            return _decode(
                self._run(
                    {
                        "action": "cached_x",
                        "alias": alias.model_dump(mode="json"),
                        "input": data.model_dump(mode="json"),
                        "policy": policy.model_dump(mode="json"),
                        "document": cached.content,
                        "metadata": cached.provider_metadata,
                    },
                    remaining=remaining,
                    cancel=cancel,
                )
            )
        except (WorkerFailure, OSError, ValueError, TypeError, KeyError):
            return Output(
                "invalid_document", metadata={"validation_diagnostic": "cached_validation_failed"}
            )
