"""Narrow OpenAI subset translation; no arbitrary upstream kwargs or state."""

import json
import time

from llm_gateway.contracts import InferenceRequest

from .engine import EngineError


def only(data, allowed):
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise EngineError("unsupported_fields")


def openai_request(operation, data, request_id):
    if not isinstance(data, dict):
        raise EngineError("object_body_required")
    common = {"request_id", "model", "stream"}
    if data.get("stream") is not None and data["stream"] is not False:
        raise EngineError("streaming_unsupported")
    context = {}
    limits = {}
    if operation == "chat":
        only(
            data,
            common
            | {
                "messages",
                "tools",
                "tool_choice",
                "response_format",
                "max_tokens",
                "max_completion_tokens",
                "parallel_tool_calls",
                "store",
            },
        )
        if data.get("store", False) is not False:
            raise EngineError("provider_state_unsupported")
        if "max_tokens" in data and "max_completion_tokens" in data:
            raise EngineError("ambiguous_output_limit")
        cap = data.get("max_completion_tokens", data.get("max_tokens"))
        if cap is not None:
            limits["max_output_tokens"] = cap
        tools = []
        supplied_tools = data.get("tools", [])
        if not isinstance(supplied_tools, list) or len(supplied_tools) > 128:
            raise EngineError("unsupported_tools")
        for tool in supplied_tools:
            only(tool, {"type", "function"})
            if tool.get("type") != "function":
                raise EngineError("unsupported_tool")
            only(tool.get("function"), {"name", "description", "parameters", "strict"})
            tools.append({"type": "function", **tool["function"]})
        choice = data.get("tool_choice", "auto")
        if isinstance(choice, dict):
            only(choice, {"type", "function"})
            only(choice.get("function"), {"name"})
            if choice.get("type") != "function":
                raise EngineError("unsupported_tool")
            choice = {"type": "function", "name": choice["function"]["name"]}
        body = {
            "kind": "chat",
            "messages": data["messages"],
            "tools": tools,
            "tool_choice": choice,
            "parallel_tool_calls": data.get("parallel_tool_calls", False),
        }
        format = data.get("response_format", {"type": "text"})
        only(format, {"type", "json_schema"})
        body["response_format"] = format.get("type", "text")
        if body["response_format"] == "json_schema":
            schema = format.get("json_schema")
            only(schema, {"name", "strict", "schema", "description"})
            if schema.get("strict", True) is not True or "description" in schema:
                raise EngineError("unsupported_schema_parameters")
            body.update(response_schema=schema["schema"], schema_name=schema["name"])
        else:
            only(format, {"type"})
    elif operation == "responses":
        only(
            data,
            common
            | {
                "input",
                "instructions",
                "tools",
                "tool_choice",
                "parallel_tool_calls",
                "store",
                "reasoning",
                "text",
                "max_output_tokens",
            },
        )
        body = {
            "kind": "responses",
            **{
                k: v
                for k, v in data.items()
                if k
                in {"input", "instructions", "tools", "tool_choice", "parallel_tool_calls", "store"}
            },
        }
        if "max_output_tokens" in data:
            limits["max_output_tokens"] = data["max_output_tokens"]
        reasoning = data.get("reasoning", {})
        only(reasoning, {"effort"})
        if reasoning:
            body["reasoning_effort"] = reasoning["effort"]
        text = data.get("text", {})
        only(text, {"verbosity", "format"})
        if "verbosity" in text:
            body["text_verbosity"] = text["verbosity"]
        if "format" in text:
            format = text["format"]
            only(format, {"type", "name", "strict", "schema"})
            if format.get("strict", True) is not True:
                raise EngineError("unsupported_schema_parameters")
            body["response_format"] = format["type"]
            if format["type"] == "json_schema":
                body.update(response_schema=format["schema"], schema_name=format["name"])
            else:
                only(format, {"type"})
    else:
        only(data, {"request_id", "model", "input", "dimensions", "encoding_format"})
        input = data["input"]
        body = {
            "kind": "embeddings",
            "input": [input] if isinstance(input, str) else input,
            **{k: data[k] for k in ("dimensions", "encoding_format") if k in data},
        }
    return InferenceRequest.model_validate(
        {
            "request_id": request_id,
            "alias": data["model"],
            "operation": operation,
            "input": body,
            "context": context,
            "limits": limits,
        }
    )


def usage(result, *, chat=False):
    if not result.receipts:
        return None
    observed = result.receipts[-1].usage
    if any(
        v is None for v in (observed.input_tokens, observed.output_tokens, observed.total_tokens)
    ):
        return None
    if chat:
        return {
            "prompt_tokens": observed.input_tokens,
            "completion_tokens": observed.output_tokens,
            "total_tokens": observed.total_tokens,
        }
    body = {
        "input_tokens": observed.input_tokens,
        "output_tokens": observed.output_tokens,
        "total_tokens": observed.total_tokens,
    }
    if observed.cache_tokens is not None:
        body["input_tokens_details"] = {"cached_tokens": observed.cache_tokens}
    if observed.reasoning_tokens is not None:
        body["output_tokens_details"] = {"reasoning_tokens": observed.reasoning_tokens}
    return body


def dashboard(result, *, latency_ms):
    receipts = result.receipts
    measured = [r.measured_usd for r in receipts]
    known = all(v is not None for v in measured)
    from llm_gateway.contracts import add_usd, usd_string

    return {
        "alias": result.model.alias,
        "provider": result.model.actual_provider,
        "model": result.model.actual_model or result.model.upstream_model,
        "generation_id": result.model.generation_id,
        "content": result.content,
        "usage": {
            **(receipts[-1].usage.model_dump(mode="json") if receipts else {}),
            "cost_usd": usd_string(add_usd(*measured)) if known and receipts else None,
        },
        "latency_ms": latency_ms,
        "attempts": len(receipts),
        "attempt_errors": result.provider_metadata.get("attempt_errors", []),
        "usage_complete": all(r.usage_complete for r in receipts),
        "provider_metadata": {
            **result.provider_metadata,
            "request_id": result.request_id,
            "trace_id": result.trace_id,
            "config_digest": result.config_digest,
            "receipt_ids": [r.receipt_id for r in receipts],
            "receipts": [r.model_dump(mode="json") for r in receipts],
        },
    }


def openai_response(result, operation):
    identifier = result.model.generation_id or "gateway_" + result.request_id
    model = result.model.actual_model or result.model.upstream_model
    text = result.content if isinstance(result.content, str) else json.dumps(result.content)
    if operation == "chat":
        message = {"role": "assistant", "content": text if result.content is not None else None}
        if result.function_calls:
            message["tool_calls"] = [
                {
                    "type": "function",
                    "id": call.call_id,
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in result.function_calls
            ]
        return {
            "id": identifier,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls"
                    if result.function_calls
                    else result.finish_reason or "stop",
                    "message": message,
                }
            ],
            "usage": usage(result, chat=True),
        }
    if operation == "embeddings":
        observed = result.receipts[-1].usage if result.receipts else None
        return {
            "object": "list",
            "model": model,
            "data": [{"object": "embedding", **row} for row in result.content or []],
            "usage": {"prompt_tokens": observed.input_tokens, "total_tokens": observed.total_tokens}
            if observed and observed.input_tokens is not None and observed.total_tokens is not None
            else None,
        }
    items = result.provider_metadata.get("output_items")
    if items is None:
        items = (
            [
                {
                    "id": "msg_" + result.request_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            ]
            if result.content is not None
            else []
        )
        items += [
            {
                "id": "fc_" + call.call_id,
                "type": "function_call",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
                "status": "completed",
            }
            for call in result.function_calls
        ]
    return {
        "id": identifier,
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "status": "completed",
        "output": items,
        "parallel_tool_calls": result.provider_metadata.get("parallel_tool_calls", False),
        "store": False,
        "usage": usage(result),
    }
