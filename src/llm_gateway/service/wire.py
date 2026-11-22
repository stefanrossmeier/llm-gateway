"""Strict compatibility contracts. Intentional legacy changes stay explicit."""

from datetime import datetime

from pydantic import Field

from llm_gateway.contracts import Message
from llm_gateway.contracts.models import Alias, Contract, Identifier, JsonObject
from llm_gateway.contracts.money import Usd


class AllowanceReview(Contract):
    config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_id: Identifier
    reason: str = Field(min_length=1, max_length=1024)


class DashboardChat(Contract):
    request_id: Identifier | None = None
    alias: Alias
    purpose: Identifier
    profile: Identifier
    run_id: Identifier | None = None
    messages: list[Message]
    response_schema_name: Alias
    response_schema: JsonObject


class SafeplaneChat(Contract):
    request_id: Identifier | None = None
    workflow_id: Identifier
    model_profile: Identifier = "default"
    stage_id: Identifier | None = None
    messages: list[Message]
    session_id: Identifier
    run_id: Identifier | None = None
    turn: int = Field(ge=1)
    timeout_seconds: float = Field(default=30, gt=0, le=3600)


class SafeplaneDecisions(Contract):
    request_id: Identifier | None = None
    model: Alias
    state: JsonObject
    questions: JsonObject
    run_id: Identifier | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=300)


class Reconcile(Contract):
    schema_version: int = Field(default=1, ge=1, le=1)
    evidence_id: Identifier
    request_id: Identifier
    generation_id: str = Field(min_length=1, max_length=512)
    measured_usd: Usd
    currency: str = Field(default="USD", pattern="^USD$")
    authority: str = Field(min_length=1, max_length=1024)
    verification: str = Field(pattern="^operator_verified_receipt$")
    provider_ref: Alias
    config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_at: datetime
