"""Private key-free validator entrypoint. Network and unbounded CPU are denied."""

import json
import resource
import socket
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def denied(*args, **kwargs):
    raise PermissionError("validation_network_denied")


def main():
    socket.getaddrinfo = denied
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.socket.sendto = denied
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    except (OSError, ValueError):
        pass  # Parent wall deadline/cancellation still kills this owned process.
    from pydantic import TypeAdapter

    from llm_gateway.config.models import AliasConfig, OperationPolicy, ProviderConfig
    from llm_gateway.contracts import CallerLimits
    from llm_gateway.contracts.models import InferenceInput
    from llm_gateway.providers.preparation import PreparationError, prepare
    from llm_gateway.service.output import Output, normalize, ordered_items
    from llm_gateway.service.x_scope import audit, validate_posts

    response = {"error": "validation_failed"}
    try:
        raw = sys.stdin.buffer.read(40 * 1024 * 1024 + 1)
        if len(raw) > 40 * 1024 * 1024:
            raise ValueError()
        message = json.loads(raw)
        alias = AliasConfig.model_validate(message["alias"])
        data = TypeAdapter(InferenceInput).validate_python(message["input"])
        policy = (
            TypeAdapter(OperationPolicy).validate_python(message["policy"])
            if message["policy"]
            else None
        )
        if message["action"] == "prepare":
            result = prepare(
                alias,
                ProviderConfig.model_validate(message["provider"]),
                data,
                operation_policy=policy,
                caller_limits=CallerLimits.model_validate(message["limits"]),
            )
            response = {"prepared": asdict(result)}
        elif message["action"] in {"normalize", "cached_x"}:
            if message["action"] == "cached_x":
                validate_posts(message["document"], data, policy)
                output = Output("valid", message["document"], metadata=message["metadata"])
                response = {"output": asdict(output)}
                encoded = json.dumps(response, ensure_ascii=False, allow_nan=False)
                if len(encoded.encode()) > 20 * 1024 * 1024:
                    raise ValueError()
                sys.stdout.write(encoded)
                return
            payload = message["payload"]
            output = normalize(payload, data, alias)
            if data.kind == "responses":
                output.metadata["output_items"] = ordered_items(payload)
                output.metadata["parallel_tool_calls"] = data.parallel_tool_calls
            if data.kind == "x_search":
                output = audit(
                    payload,
                    output,
                    data,
                    alias,
                    policy,
                    quota_period=message["quota_period"],
                    request_identity=message["request_identity"],
                )
            result = asdict(output)
            result["function_calls"] = [
                call.model_dump(mode="json") for call in output.function_calls
            ]
            response = {"output": result}
    except PreparationError as error:
        response = {"error": str(error)}
    except Exception:
        pass
    encoded = json.dumps(response, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 20 * 1024 * 1024:
        encoded = '{"error":"validation_output_too_large"}'
    sys.stdout.write(encoded)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
