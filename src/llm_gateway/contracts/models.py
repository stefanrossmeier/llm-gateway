"""Versioned public contracts; no service/provider imports or permissive kwargs."""

import json
from datetime import date
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from .money import TokenCount, Usd


def _path_identifier(value):
    if value in {".", ".."}:
        raise ValueError("identifier cannot be a reserved path segment")
    return value


Identifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"),
    AfterValidator(_path_identifier),
]
Alias = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
XHandle = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_]+$")]
Operation = Literal["chat", "responses", "decisions", "x_search", "embeddings"]
JsonObject = dict[str, JsonValue]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def validate_json_tree(value: JsonValue, *, schema: bool = False) -> None:
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 64 or nodes > 16384:
            raise ValueError("JSON exceeds depth/node limits")
        if isinstance(item, dict):
            for key in ("$ref", "$dynamicRef") if schema else ():
                if key in item:
                    reference = item[key]
                    if not isinstance(reference, str) or not reference.startswith("#"):
                        raise ValueError("only document-local schema references are allowed")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


class MessageFunction(Contract):
    name: Alias
    arguments: str


class MessageToolCall(Contract):
    id: Identifier
    type: Literal["function"] = "function"
    function: MessageFunction


class TextPart(Contract):
    type: Literal["text", "input_text", "output_text"]
    text: str


class Message(Contract):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[TextPart] | None
    tool_call_id: Identifier | None = None
    tool_calls: list[MessageToolCall] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def tool_protocol(self):
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages may contain function calls")
        if self.content is None and not (self.role == "assistant" and self.tool_calls):
            raise ValueError("null message content requires assistant function calls")
        if self.role == "tool" and self.tool_call_id is None:
            raise ValueError("tool response requires tool_call_id")
        if self.role != "tool" and self.tool_call_id is not None:
            raise ValueError("tool_call_id is only valid for tool responses")
        return self


class FunctionResultInput(Contract):
    type: Literal["function_call_output"] = "function_call_output"
    call_id: Identifier
    output: str


class FunctionInvocationInput(Contract):
    type: Literal["function_call"] = "function_call"
    call_id: Identifier
    name: Alias
    arguments: str


class FunctionTool(Contract):
    type: Literal["function"] = "function"
    name: Alias
    description: str | None = Field(default=None, max_length=4096)
    parameters: JsonObject
    strict: StrictBool = True

    @model_validator(mode="after")
    def local_schema(self):
        validate_json_tree(self.parameters, schema=True)
        return self


class FunctionChoice(Contract):
    type: Literal["function"] = "function"
    name: Alias


class ChatInput(Contract):
    kind: Literal["chat"] = "chat"
    messages: list[Message] = Field(min_length=1, max_length=1000)
    response_format: Literal["text", "json_object", "json_schema"] = "text"
    response_schema: JsonObject | None = None
    schema_name: Alias = "gateway_response"
    tools: list[FunctionTool] = Field(default_factory=list, max_length=128)
    tool_choice: Literal["none", "auto", "required"] | FunctionChoice = "auto"
    parallel_tool_calls: StrictBool = False

    @model_validator(mode="after")
    def document_schema(self):
        if self.response_format == "json_schema" and self.response_schema is None:
            raise ValueError("json_schema format requires response_schema")
        if self.response_schema is not None:
            validate_json_tree(self.response_schema, schema=True)
        return self


class ResponsesInput(Contract):
    kind: Literal["responses"] = "responses"
    instructions: str = ""
    input: str | list[Message | FunctionResultInput | FunctionInvocationInput]
    tools: list[FunctionTool] = Field(default_factory=list, max_length=128)
    tool_choice: Literal["none", "auto", "required"] | FunctionChoice = "auto"
    parallel_tool_calls: StrictBool = False
    store: Literal[False] = False
    reasoning_effort: Alias | None = None
    text_verbosity: Literal["low", "medium", "high"] | None = None
    response_schema: JsonObject | None = None
    response_format: Literal["text", "json_object", "json_schema"] = "text"
    schema_name: Alias = "gateway_response"

    @field_validator("store", mode="before")
    @classmethod
    def never_store(cls, value):
        if value is not False:
            raise ValueError("store must be the boolean false")
        return value

    @model_validator(mode="after")
    def response_schema_local(self):
        if self.response_format == "json_schema" and self.response_schema is None:
            raise ValueError("json_schema format requires response_schema")
        if self.response_schema is not None:
            validate_json_tree(self.response_schema, schema=True)
        return self


class DecisionsInput(Contract):
    kind: Literal["decisions"] = "decisions"
    state: JsonObject
    questions: JsonObject

    @model_validator(mode="after")
    def bounded_state(self):
        validate_json_tree(self.state)
        validate_json_tree(self.questions)
        return self


class XSearchInput(Contract):
    kind: Literal["x_search"] = "x_search"
    input: str = Field(max_length=5000)
    allowed_x_handles: list[XHandle] = Field(min_length=1, max_length=64)
    from_date: date
    to_date: date
    response_schema: JsonObject
    schema_name: Alias = "gateway_response"

    @model_validator(mode="after")
    def dates_and_handles(self):
        if self.from_date >= self.to_date:
            raise ValueError("to_date must be exclusive and later than from_date")
        handles = [handle.casefold() for handle in self.allowed_x_handles]
        if len(set(handles)) != len(handles):
            raise ValueError("duplicate handles")
        validate_json_tree(self.response_schema, schema=True)
        return self


class EmbeddingsInput(Contract):
    kind: Literal["embeddings"] = "embeddings"
    input: list[str] = Field(min_length=1, max_length=256)
    dimensions: StrictInt | None = Field(default=None, gt=0, le=65536)
    encoding_format: Literal["float", "base64"] = "float"


class RequestContext(Contract):
    run_id: Identifier | None = None
    session_id: Identifier | None = None
    workflow_id: Identifier | None = None
    stage_id: Identifier | None = None
    turn: StrictInt | None = Field(default=None, ge=1)
    role: Identifier | None = None
    purpose: Identifier | None = None
    profile: Identifier | None = None
    phase: Identifier = "task"
    test_group: Literal["offline", "live_free", "live_paid"] | None = None
    tags: dict[str, str] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def bounded_tags(self):
        if any(len(key) > 64 or len(value) > 512 for key, value in self.tags.items()):
            raise ValueError("context tag is too large")
        if len(json.dumps(self.tags, ensure_ascii=False).encode()) > 32768:
            raise ValueError("context tags exceed 32 KiB")
        return self


class CallerLimits(Contract):
    timeout_seconds: float | None = Field(default=None, gt=0, le=3600)
    max_output_tokens: TokenCount | None = None
    max_attempts: StrictInt | None = Field(default=None, ge=1, le=10)
    max_usd: Usd | None = None


InferenceInput = Annotated[
    ChatInput | ResponsesInput | DecisionsInput | XSearchInput | EmbeddingsInput,
    Field(discriminator="kind"),
]


class InferenceRequest(Contract):
    schema_version: Literal[1] = 1
    request_id: Identifier
    alias: Alias
    operation: Operation
    input: InferenceInput
    context: RequestContext = Field(default_factory=RequestContext)
    limits: CallerLimits = Field(default_factory=CallerLimits)

    @model_validator(mode="after")
    def consistent_operation(self):
        if self.operation != self.input.kind:
            raise ValueError("input kind does not match operation")
        pending = [self.model_dump(mode="json")]
        try:
            while pending:
                value = pending.pop()
                if isinstance(value, str):
                    value.encode("utf-8")
                elif isinstance(value, dict):
                    pending.extend(value.keys())
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
        except UnicodeError:
            raise ValueError("request text must be valid UTF-8") from None
        return self


class TokenUsage(Contract):
    x_posts_fetched: TokenCount | None = None
    x_profiles_fetched: TokenCount | None = None
    input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None
    total_tokens: TokenCount | None = None
    reasoning_tokens: TokenCount | None = None
    cache_tokens: TokenCount | None = None


class ModelIdentity(Contract):
    alias: Alias
    provider_ref: Alias
    configured_model: str
    upstream_model: str
    actual_model: str | None = None
    actual_provider: str | None = None
    generation_id: str | None = None


class SafeError(Contract):
    code: Alias
    message: str = Field(max_length=1024)
    advice: Literal["query_receipt", "reconcile", "correct_request"]
    trace_id: Identifier
    receipt_id: Identifier | None = None


class AttemptReceipt(Contract):
    schema_version: Literal[1] = 1
    receipt_id: Identifier
    project_id: Alias
    principal_id: Identifier
    request_id: Identifier
    attempt_id: Identifier
    attempt: StrictInt = Field(ge=1, le=10)
    config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    model: ModelIdentity
    usage: TokenUsage = Field(default_factory=TokenUsage)
    evidence_status: Literal[
        "measured",
        "estimated",
        "unavailable",
        "explicit_free",
        "cached_reuse",
        "subscription_covered",
        "simulated",
        "not_dispatched",
    ] = "unavailable"
    currency: Literal["USD"] = "USD"
    measured_usd: Usd | None = None
    estimated_usd: Usd | None = None
    protected_debit_usd: Usd
    contingent_exposure_usd: Usd
    pricing_source: str | None = None
    reservation_kind: Literal["bound", "risk_allowance", "simulated", "legacy_unclassified"] = (
        "bound"
    )
    usage_complete: StrictBool = False
    reconciliation_required: StrictBool = False

    @model_validator(mode="after")
    def evidence_consistency(self):
        if self.evidence_status == "measured" and self.measured_usd is None:
            raise ValueError("measured evidence requires measured_usd")
        if self.evidence_status == "estimated" and self.estimated_usd is None:
            raise ValueError("estimated evidence requires estimated_usd")
        if self.usage_complete and self.evidence_status in {"unavailable", "estimated"}:
            raise ValueError("unavailable/estimated evidence is incomplete")
        return self


class FunctionCall(Contract):
    call_id: str
    name: str
    arguments: str
    parsed_arguments: JsonObject | None = None
    diagnostic: str | None = None


class InferenceResult(Contract):
    schema_version: Literal[1] = 1
    request_id: Identifier
    trace_id: Identifier
    project_id: Alias
    config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_status: Literal["in_progress", "completed", "failed", "cancelled", "timed_out"]
    output_status: Literal[
        "pending",
        "valid",
        "refused",
        "truncated",
        "invalid_document",
        "invalid_tool_arguments",
        "provider_failed",
        "unavailable",
    ]
    content: JsonValue = None
    function_calls: list[FunctionCall] = Field(default_factory=list)
    model: ModelIdentity
    receipts: list[AttemptReceipt] = Field(default_factory=list)
    error: SafeError | None = None
    finish_reason: str | None = None
    provider_metadata: JsonObject = Field(default_factory=dict)
