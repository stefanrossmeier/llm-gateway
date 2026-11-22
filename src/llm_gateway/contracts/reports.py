"""Lean typed inspection/report views; no configuration or provider dependency."""

from typing import Literal

from pydantic import Field, StrictBool, StrictInt

from .models import Alias, Contract, Identifier, JsonObject, Operation
from .money import Usd


class UsageTotals(Contract):
    attempts: StrictInt = Field(ge=0)
    measured_usd: Usd
    estimated_usd: Usd
    protected_debit_usd: Usd
    contingent_exposure_usd: Usd
    unknown_attempts: StrictInt = Field(ge=0)
    usage_complete: StrictBool
    overrun: StrictBool
    measured_attempts: StrictInt = Field(default=0, ge=0)
    estimated_attempts: StrictInt = Field(default=0, ge=0)
    unsettled_attempts: StrictInt = Field(default=0, ge=0)


class BucketReport(Contract):
    bucket_id: str
    policy_id: Alias
    period_key: str
    cap_usd: Usd
    protected_debit_usd: Usd
    remaining_usd: Usd


class UsageReport(UsageTotals):
    project_id: Alias | None = None
    currency: Literal["USD"] = "USD"
    timezone: str = "UTC"
    attempt_ids: list[Identifier]
    buckets: list[BucketReport]
    groups: dict[str, UsageTotals] = Field(default_factory=dict)


class AliasInspection(Contract):
    alias: Alias
    operation: Operation
    provider_ref: Alias
    provider_kind: Literal["openai", "openrouter"]
    upstream_model: str
    capabilities: list[str]
    limits: JsonObject


class AliasReport(Contract):
    config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    aliases: list[AliasInspection]


class Readiness(Contract):
    version: str | None = None
    supports_config_precondition: StrictBool = False
    principal_id: Alias | None = None
    status: Literal["ready"]
    project_id: Alias
    config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
