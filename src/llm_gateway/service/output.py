"""Output validity is orthogonal to observed provider billing evidence."""

import base64
import binascii
import json
import math
import struct
from dataclasses import dataclass, field
from decimal import Decimal

from jsonschema import Draft202012Validator

from llm_gateway.contracts.models import FunctionCall

INVALID = object()


@dataclass
class Output:
    status: str
    content: object = None
    function_calls: list[FunctionCall] = field(default_factory=list)
    finish_reason: str | None = None
    metadata: dict = field(default_factory=dict)


def ordered_items(payload):
    """Allowlisted Responses items preserve order without provider passthrough."""
    items = []
    for item in payload.get("output") or []:
        if item.get("type") == "function_call":
            items.append(
                {
                    key: item[key]
                    for key in ("id", "type", "call_id", "name", "arguments", "status")
                    if key in item
                }
            )
        elif item.get("type") == "message":
            message = {key: item[key] for key in ("id", "type", "role", "status") if key in item}
            content = []
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    content.append(
                        {
                            "type": "output_text",
                            "text": part.get("text", ""),
                            "annotations": [
                                {
                                    key: annotation[key]
                                    for key in ("type", "url", "title", "start_index", "end_index")
                                    if key in annotation
                                }
                                for annotation in part.get("annotations") or []
                                if annotation.get("type") == "url_citation"
                            ],
                        }
                    )
                elif part.get("type") == "refusal":
                    content.append({"type": "refusal", "refusal": part.get("refusal", "")})
            items.append({**message, "content": content})
    return items


def _document(text, schema):
    try:
        content = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if schema is not None:
            Draft202012Validator(schema).validate(content)
        return content
    except (RecursionError, MemoryError):
        raise ValueError("validation_resource_limit") from None
    except Exception:
        return INVALID


def normalize(payload: dict, data, alias) -> Output:
    operation = data.kind
    calls, text, finish, refused, truncated = [], "", None, False, False
    if operation == "decisions":
        answers = payload.get("answers")
        if not isinstance(answers, dict) or set(answers) != set(data.questions):
            return Output("invalid_document")
        refused = any(isinstance(a, dict) and a.get("type") == "refusal" for a in answers.values())
        return Output("refused" if refused else "valid", {"answers": answers})
    if operation == "embeddings":
        try:
            vectors = payload["data"]
            if len(vectors) != len(data.input):
                raise ValueError()
            ordered = sorted(vectors, key=lambda row: row["index"])
            if [r["index"] for r in ordered] != list(range(len(data.input))):
                raise ValueError()
            dimensions = data.dimensions or alias.limits.embedding_dimensions
            size = None
            result = []
            for row in ordered:
                vector = row["embedding"]
                encoded = isinstance(vector, str)
                if encoded:
                    raw = base64.b64decode(vector, validate=True)
                    vector = list(struct.unpack("<" + "f" * (len(raw) // 4), raw))
                if not isinstance(vector, list) or not vector:
                    raise ValueError()
                if (
                    size is not None
                    and len(vector) != size
                    or dimensions
                    and len(vector) != dimensions
                ):
                    raise ValueError()
                size = len(vector)
                vector = [
                    float(v)
                    for v in vector
                    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)
                ]
                if len(vector) != size or not all(math.isfinite(v) for v in vector):
                    raise ValueError()
                result.append(
                    {"index": row["index"], "embedding": row["embedding"] if encoded else vector}
                )
            return Output("valid", result)
        except (KeyError, TypeError, ValueError, binascii.Error, struct.error):
            return Output("invalid_document")
    if operation == "chat":
        choices = payload.get("choices") or []
        if len(choices) != 1 or not isinstance(choices[0], dict):
            return Output("invalid_document")
        choice = choices[0]
        message = choice.get("message") or {}
        text, finish = message.get("content") or "", choice.get("finish_reason")
        refused = bool(message.get("refusal")) or finish == "content_filter"
        truncated = finish == "length"
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            calls.append(
                {
                    "call_id": call.get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", ""),
                }
            )
    else:
        finish = payload.get("status")
        truncated = finish == "incomplete"
        if finish not in {"completed", "incomplete"}:
            return Output("invalid_document")
        for item in payload.get("output") or []:
            if item.get("type") == "function_call":
                calls.append(
                    {
                        "call_id": item.get("call_id", ""),
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", ""),
                    }
                )
            elif item.get("type") == "message":
                for part in item.get("content") or []:
                    if part.get("type") == "output_text":
                        text += part.get("text", "")
                    elif part.get("type") == "refusal":
                        refused = True
    if refused:
        return Output("refused", finish_reason=finish)
    if truncated:
        return Output("truncated", text, finish_reason=finish)
    tools = {tool.name: tool for tool in getattr(data, "tools", [])}
    functions = []
    invalid = False
    for call in calls:
        diagnostic, parsed = None, None
        if not all(isinstance(v, str) for v in call.values()):
            call = {k: v if isinstance(v, str) else "" for k, v in call.items()}
            diagnostic = "invalid_function_shape"
        elif call["name"] not in tools:
            diagnostic = "undeclared_function"
        else:
            parsed = _document(call["arguments"], tools[call["name"]].parameters)
            if not isinstance(parsed, dict):
                diagnostic, parsed = "invalid_function_arguments", None
        invalid |= diagnostic is not None
        functions.append(FunctionCall(**call, parsed_arguments=parsed, diagnostic=diagnostic))
    if invalid:
        return Output("invalid_tool_arguments", text or None, functions, finish)
    if len(functions) > 1 and not getattr(
        data, "parallel_tool_calls", alias.generation.parallel_tool_calls
    ):
        return Output("invalid_tool_arguments", text or None, functions, finish)
    choice = getattr(data, "tool_choice", "auto")
    if choice != "auto" and choice != "none":
        if (
            not functions
            or hasattr(choice, "name")
            and any(f.name != choice.name for f in functions)
        ):
            return Output("invalid_tool_arguments", text or None, functions, finish)
    if choice == "none" and functions:
        return Output("invalid_tool_arguments", text or None, functions, finish)
    if functions:
        return Output("valid", text or None, functions, finish)
    if not isinstance(text, str):
        return Output("invalid_document", finish_reason=finish)
    schema = getattr(data, "response_schema", None)
    response_format = max(
        (getattr(data, "response_format", "text"), alias.generation.response_format or "text"),
        key=["text", "json_object", "json_schema"].index,
    )
    if schema is not None or response_format in {"json_object", "json_schema"}:
        document = _document(text, schema)
        if (
            document is INVALID
            or response_format == "json_object"
            and not isinstance(document, dict)
        ):
            return Output("invalid_document", finish_reason=finish)
        return Output("valid", document, finish_reason=finish)
    return Output("valid", text, finish_reason=finish)
