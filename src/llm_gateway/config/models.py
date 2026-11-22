"""Schema v1 for configuration supplied and owned by a consuming project."""

import re
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, StrictBool, StrictInt, ValidationInfo, field_validator, model_validator

from llm_gateway.contracts.models import Alias, Contract, Identifier, Operation, XHandle
from llm_gateway.contracts.money import TokenCount, Usd

CAPABILITY_REVISION = "litellm-1.109.0/gateway-v5"
OPERATION_CAPABILITIES = {
    "chat": {"plain_text", "json_object", "json_schema", "function_calls", "reasoning_effort"},
    "responses": {
        "plain_text",
        "json_object",
        "json_schema",
        "function_calls",
        "reasoning_effort",
        "text_verbosity",
    },
    "decisions": set(),
    "x_search": {"plain_text", "json_schema", "native_x_search"},
    "embeddings": {"embedding_dimensions"},
}
Capability = Literal[
    "plain_text",
    "json_object",
    "json_schema",
    "function_calls",
    "reasoning_effort",
    "text_verbosity",
    "native_x_search",
    "embedding_dimensions",
]
Permission = Literal[
    "infer",
    "receipts:own",
    "usage:project",
    "admin:reload",
    "admin:reconcile",
    "admin:backup",
]
EnvName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")]


def validate_upstream(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", value):
        raise ValueError("invalid upstream model identifier")
    if "://" in value or ".." in value:
        raise ValueError("upstream model cannot be a transport or path override")
    return value


def validate_provider_model(kind: str, model: str) -> None:
    # OpenRouter's :online variant enables a billed server-side plugin even
    # without an explicit tools/plugins field. Colon variants can be stacked.
    if kind == "openrouter" and "online" in model.casefold().split(":")[1:]:
        raise ValueError("implicit_web_search_variant_forbidden")


class CredentialReference(Contract):
    env_var: EnvName | None = None
    file_env_var: EnvName | None = None
    file_path: str | None = None

    @field_validator("env_var", "file_env_var")
    @classmethod
    def dedicated_credential_variable(cls, value, info: ValidationInfo):
        if info.context and info.context.get("legacy_snapshot"):
            return value
        if value is not None and (
            value.startswith(("PYTHON", "LD_", "DYLD_", "SSL_", "LITELLM_", "HF_"))
            or value
            in {
                "HOME",
                "PATH",
                "TMPDIR",
                "TMP",
                "TEMP",
                "LANG",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
                "DO_NOT_TRACK",
                "REQUESTS_CA_BUNDLE",
                "CURL_CA_BUNDLE",
                "OPENAI_API_BASE",
                "OPENAI_BASE_URL",
                "OPENAI_ORG_ID",
                "OPENAI_ORGANIZATION",
                "OPENAI_PROJECT",
                "OPENROUTER_BASE_URL",
            }
        ):
            raise ValueError("credential variable cannot override runtime controls")
        return value

    @model_validator(mode="after")
    def explicit_reference(self):
        if not any((self.env_var, self.file_env_var, self.file_path)):
            raise ValueError("credential reference is required")
        if self.file_path is not None and not self.file_path.startswith("/"):
            raise ValueError("secret file path must be absolute")
        return self


class ProviderConfig(Contract):
    kind: Literal["openai", "openrouter"]
    credential: CredentialReference | None = None
    enabled: StrictBool = True
    application_name: str | None = Field(default=None, max_length=128)


class PrincipalConfig(Contract):
    token_file: str
    aliases: list[Alias] = Field(default_factory=list)
    operations: list[Operation] = Field(default_factory=list)
    permissions: list[Permission] = Field(default_factory=list)
    run_id: Identifier | None = None
    allow_client_run_ids: StrictBool = False
    phase: Identifier | None = None

    @field_validator("token_file")
    @classmethod
    def absolute_token_file(cls, value):
        if not value.startswith("/"):
            raise ValueError("token file must be absolute")
        return value


class AccessConfig(Contract):
    principals: dict[Alias, PrincipalConfig] = Field(default_factory=dict)
    owner_only_ipc: StrictBool = False


class ModelLimits(Contract):
    max_input_bytes: StrictInt = Field(gt=0, le=16777216)
    max_input_tokens: StrictInt = Field(gt=0)
    attempt_timeout_seconds: float = Field(gt=0, le=3600)
    request_timeout_seconds: float = Field(gt=0, le=3600)
    max_concurrency: StrictInt = Field(gt=0, le=64)
    max_output_tokens: StrictInt | None = Field(default=None, gt=0)
    max_reasoning_tokens: TokenCount | None = None
    max_tool_calls: TokenCount = 0
    max_batch_items: StrictInt = Field(default=256, gt=0, le=256)
    embedding_dimensions: StrictInt | None = Field(default=None, gt=0, le=65536)

    @model_validator(mode="after")
    def deadlines(self):
        if self.attempt_timeout_seconds > self.request_timeout_seconds:
            raise ValueError("attempt timeout exceeds request timeout")
        return self


class GenerationOptions(Contract):
    temperature: float | None = Field(default=None, ge=0, le=2)
    response_format: Literal["text", "json_object", "json_schema"] | None = None
    reasoning_effort: Alias | None = None
    text_verbosity: Literal["low", "medium", "high"] | None = None
    parallel_tool_calls: StrictBool = False
    store: Literal[False] = False

    @field_validator("store", mode="before")
    @classmethod
    def no_provider_storage(cls, value):
        if value is not False:
            raise ValueError("store must be false")
        return value


class PricingConfig(Contract):
    revision: Identifier
    bound_source: str = Field(min_length=1, max_length=4096)
    free_only: StrictBool = False
    input_usd_per_token: Usd | None = None
    output_usd_per_token: Usd | None = None
    reasoning_usd_per_token: Usd | None = None
    cached_input_usd_per_token: Usd | None = None
    tool_fees_usd: dict[Alias, Usd] = Field(default_factory=dict)
    # Legacy snapshot compatibility only. New native X admission never uses
    # search-call fees/counts as a bound on fetched-item charges.
    tool_fee_calls: StrictInt | None = Field(default=None, ge=1, le=64)
    x_post_usd: Usd | None = None
    x_profile_usd: Usd | None = None
    x_request_allowance_usd: Usd | None = None
    accept_unbounded_x_charges: StrictBool = False
    ceiling_usd: Usd | None = None
    input_bound: Literal["utf8_bytes_with_framing"] | None = None
    framing_tokens_per_item: TokenCount | None = None

    @field_validator("tool_fees_usd")
    @classmethod
    def supported_fee_components(cls, value, info: ValidationInfo):
        if info.context and info.context.get("legacy_snapshot"):
            return value
        if set(value) - {"native_x_search"}:
            raise ValueError("unsupported billed fee component")
        return value

    @model_validator(mode="after")
    def no_paid_free_config(self):
        if self.free_only:
            prices = [
                self.input_usd_per_token,
                self.output_usd_per_token,
                self.reasoning_usd_per_token,
                self.cached_input_usd_per_token,
                self.ceiling_usd,
                self.x_post_usd,
                self.x_profile_usd,
                self.x_request_allowance_usd,
                *self.tool_fees_usd.values(),
            ]
            if any(price is not None and price != 0 for price in prices):
                raise ValueError("free-only configuration cannot contain nonzero fees")
        return self


class FallbackRoute(Contract):
    provider_ref: Alias
    upstream_model: str = Field(min_length=1, max_length=256)
    capabilities: list[Capability]
    pricing: PricingConfig

    _valid_model = field_validator("upstream_model")(validate_upstream)


class AttemptPolicy(Contract):
    max_attempts: StrictInt = Field(default=1, ge=1, le=10)
    backoff_seconds: float = Field(default=0, ge=0, le=60)
    unknown_charge_policy: Literal["stop", "bounded_exposure"] = "stop"
    fallback_routes: list[FallbackRoute] = Field(default_factory=list, max_length=9)


class AliasConfig(Contract):
    provider_ref: Alias
    upstream_model: str = Field(min_length=1, max_length=256)
    operation: Operation
    capabilities: list[Capability]
    limits: ModelLimits
    generation: GenerationOptions = Field(default_factory=GenerationOptions)
    attempt_policy: AttemptPolicy = Field(default_factory=AttemptPolicy)
    budget_policy_refs: list[Alias] = Field(default_factory=list)
    pricing: PricingConfig | None = None
    operation_policy_ref: Alias | None = None

    _valid_model = field_validator("upstream_model")(validate_upstream)

    @model_validator(mode="after")
    def operation_limits(self, info: ValidationInfo):
        if info.context and info.context.get("legacy_snapshot"):
            return self
        if self.operation != "x_search" and (
            self.limits.max_tool_calls or self.pricing and self.pricing.tool_fees_usd
        ):
            raise ValueError("native tool fees/limits require the X operation")
        if self.operation == "decisions" and (
            self.limits.max_output_tokens is not None
            or self.limits.max_reasoning_tokens is not None
        ):
            raise ValueError("Decisions cannot enforce output/reasoning token caps")
        if (
            self.attempt_policy.unknown_charge_policy == "bounded_exposure"
            and self.operation != "chat"
        ):
            raise ValueError("bounded unknown retries are limited to chat ranking")
        if (
            self.attempt_policy.unknown_charge_policy == "bounded_exposure"
            and self.attempt_policy.fallback_routes
        ):
            raise ValueError("unknown retries must retain the same model route")
        if not set(self.capabilities).issubset(OPERATION_CAPABILITIES[self.operation]):
            raise ValueError("operation does not support the configured capability")
        format_capability = {
            "text": "plain_text",
            "json_object": "json_object",
            "json_schema": "json_schema",
        }.get(self.generation.response_format)
        if format_capability and format_capability not in self.capabilities:
            raise ValueError("configured response format is not granted")
        if self.generation.reasoning_effort and "reasoning_effort" not in self.capabilities:
            raise ValueError("configured reasoning effort is not granted")
        if self.generation.text_verbosity and "text_verbosity" not in self.capabilities:
            raise ValueError("configured verbosity is not granted")
        if (
            self.operation not in {"embeddings", "decisions"}
            and self.limits.max_output_tokens is None
        ):
            raise ValueError("generative operations require a positive output limit")
        if self.operation == "x_search" and (
            not self.operation_policy_ref
            or "native_x_search" not in self.capabilities
            or not self.limits.max_tool_calls
            or self.attempt_policy.max_attempts != 1
            or self.attempt_policy.fallback_routes
            or self.attempt_policy.unknown_charge_policy != "stop"
        ):
            raise ValueError("X search requires an explicit bounded one-attempt policy")
        if len(self.attempt_policy.fallback_routes) >= self.attempt_policy.max_attempts:
            raise ValueError("fallback routes exceed the total attempt limit")
        return self


class BudgetPolicy(Contract):
    scope: Literal["project", "credential", "run", "workflow", "alias"]
    period: Literal["call", "run", "day", "month"]
    cap_usd: Usd


class XSearchPolicy(Contract):
    kind: Literal["x_search"] = "x_search"
    allowed_x_handles: list[XHandle] = Field(min_length=1, max_length=64)
    max_handles: StrictInt = Field(gt=0, le=64)
    max_utc_days: StrictInt = Field(gt=0, le=31)
    max_posts: StrictInt = Field(gt=0, le=100)
    max_post_characters: StrictInt = Field(gt=0, le=16384)
    max_posts_per_author: StrictInt | None = Field(default=None, gt=0, le=100)
    provider_order: list[str] = Field(min_length=1, max_length=5)
    allowed_domains: list[Literal["x.com", "twitter.com"]] = Field(min_length=1)
    daily_attempts: Literal[1] = 1
    allow_subset_cache_reuse: StrictBool = False
    enable_image_understanding: Literal[False] = False
    enable_video_understanding: Literal[False] = False

    @model_validator(mode="after")
    def unique_handles(self):
        handles = [h.casefold() for h in self.allowed_x_handles]
        if len(set(handles)) != len(handles):
            raise ValueError("duplicate configured X handles")
        return self


class DecisionsPolicy(Contract):
    kind: Literal["decisions"] = "decisions"
    max_questions: StrictInt = Field(gt=0, le=100)
    state_accounting: Literal["per_question", "shared_once"] = "per_question"


class EmbeddingsPolicy(Contract):
    kind: Literal["embeddings"] = "embeddings"
    dimensions: StrictInt = Field(gt=0, le=65536)
    max_batch_items: StrictInt = Field(gt=0, le=256)


OperationPolicy = Annotated[
    XSearchPolicy | DecisionsPolicy | EmbeddingsPolicy, Field(discriminator="kind")
]


class FakeConfig(Contract):
    fixture_root: str
    responses: dict[Alias, dict[Identifier, list[str]]]


class CompatibilityConfig(Contract):
    safeplane_profiles: dict[Identifier, dict[Identifier, Alias]] = Field(default_factory=dict)
    dashboard_profiles: dict[Identifier, list[Alias]] = Field(default_factory=dict)


class EvidencePolicy(Contract):
    retain_results: StrictBool = False
    retain_prompts: StrictBool = False
    result_ttl_seconds: StrictInt = Field(default=0, ge=0, le=604800)
    cache_enabled: StrictBool = False

    @model_validator(mode="after")
    def result_retention(self):
        if (self.retain_results or self.retain_prompts) and not self.result_ttl_seconds:
            raise ValueError("content retention needs an explicit positive TTL")
        if self.cache_enabled and not self.retain_results:
            raise ValueError("cache requires private result retention")
        return self


class ProjectConfig(Contract):
    schema_version: Literal[1]
    project_id: Alias
    mode: Literal["fake", "live"]
    timezone: str = "UTC"
    providers: dict[Alias, ProviderConfig] = Field(min_length=1, max_length=64)
    aliases: dict[Alias, AliasConfig] = Field(min_length=1, max_length=256)
    budgets: dict[Alias, BudgetPolicy] = Field(default_factory=dict)
    access: AccessConfig = Field(default_factory=AccessConfig)
    operation_policies: dict[Alias, OperationPolicy] = Field(default_factory=dict)
    compatibility: CompatibilityConfig = Field(default_factory=CompatibilityConfig)
    overrides: dict[Alias, EnvName] = Field(default_factory=dict)
    fake: FakeConfig | None = None
    evidence_policy: EvidencePolicy = Field(default_factory=EvidencePolicy)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("unknown IANA timezone") from error
        return value

    @model_validator(mode="after")
    def cross_references_and_grants(self, info: ValidationInfo):
        if info.context and info.context.get("legacy_snapshot"):
            return self
        if "owner-ipc" in self.access.principals:
            raise ValueError("owner-ipc is a reserved private transport principal")
        if self.mode == "live" and not (self.access.principals or self.access.owner_only_ipc):
            raise ValueError("live mode requires explicit authenticated access")
        for profile in self.aliases.values():
            if profile.provider_ref not in self.providers:
                raise ValueError("alias provider reference is unknown")
            provider = self.providers[profile.provider_ref]
            validate_provider_model(provider.kind, profile.upstream_model)
            if not provider.enabled:
                raise ValueError("alias references disabled provider")
            if self.mode == "live" and provider.credential is None:
                raise ValueError("enabled provider needs a credential reference")
            if profile.upstream_model.startswith(("https:", "http:", "/")):
                raise ValueError("upstream model cannot supply a transport URL/path")
            if provider.kind == "openai" and "/" in profile.upstream_model:
                raise ValueError("provider/model route mismatch")
            if any(budget not in self.budgets for budget in profile.budget_policy_refs):
                raise ValueError("alias budget reference is unknown")
            policy = self.operation_policies.get(profile.operation_policy_ref)
            if profile.operation_policy_ref and (
                policy is None or policy.kind != profile.operation
            ):
                raise ValueError("operation policy kind/reference mismatch")
            if self.mode == "live":
                if profile.pricing is None:
                    raise ValueError("live inference needs explicit pricing evidence")
                if not profile.pricing.free_only:
                    selected = [self.budgets[b] for b in profile.budget_policy_refs]
                    if not selected or any(b.cap_usd <= 0 for b in selected):
                        raise ValueError("paid routes require positive budget policies")
                    if not {"call", "run"}.issubset({b.period for b in selected}):
                        raise ValueError("paid routes require call and run caps")
            if (
                profile.pricing
                and profile.pricing.free_only
                and (
                    provider.kind != "openrouter"
                    or not (
                        profile.upstream_model == "openrouter/free"
                        or profile.upstream_model.endswith(":free")
                    )
                    or "native_x_search" in profile.capabilities
                    or profile.limits.max_tool_calls
                    or profile.attempt_policy.fallback_routes
                )
            ):
                raise ValueError("free-only alias can only reach approved fee-free routes")
            for fallback in profile.attempt_policy.fallback_routes:
                if fallback.provider_ref not in self.providers:
                    raise ValueError("fallback provider reference is unknown")
                target = self.providers[fallback.provider_ref]
                validate_provider_model(target.kind, fallback.upstream_model)
                if not target.enabled:
                    raise ValueError("fallback provider is disabled")
                if fallback.upstream_model.startswith(("https:", "http:", "/")):
                    raise ValueError("fallback model cannot supply an endpoint/path")
                if target.kind == "openai" and "/" in fallback.upstream_model:
                    raise ValueError("fallback provider/model mismatch")
                if fallback.pricing.free_only and (
                    target.kind != "openrouter"
                    or not (
                        fallback.upstream_model == "openrouter/free"
                        or fallback.upstream_model.endswith(":free")
                    )
                    or profile.limits.max_tool_calls
                    or "native_x_search" in fallback.capabilities
                ):
                    raise ValueError("fallback free-only pricing requires a fee-free route")
                if not set(profile.capabilities).issubset(fallback.capabilities):
                    raise ValueError("fallback does not preserve required capabilities")
        if any(alias not in self.aliases for alias in self.overrides):
            raise ValueError("operator override references an unknown alias")
        for principal in self.access.principals.values():
            if not set(principal.aliases).issubset(self.aliases):
                raise ValueError("principal references an unknown project alias")
            if principal.allow_client_run_ids:
                if principal.run_id is not None:
                    raise ValueError("client run IDs cannot override a fixed run binding")
                for name in principal.aliases:
                    profile = self.aliases[name]
                    if self.mode == "live" and profile.pricing and not profile.pricing.free_only:
                        if not any(
                            self.budgets[b].period in {"day", "month"}
                            for b in profile.budget_policy_refs
                        ):
                            raise ValueError("client run IDs require a persistent day or month cap")
        targets = [
            target
            for mapping in self.compatibility.safeplane_profiles.values()
            for target in mapping.values()
        ]
        targets += [
            target for grants in self.compatibility.dashboard_profiles.values() for target in grants
        ]
        if any(target not in self.aliases for target in targets):
            raise ValueError("compatibility mapping references an unknown project alias")
        if self.fake and any(alias not in self.aliases for alias in self.fake.responses):
            raise ValueError("fixture references an unknown alias")
        return self
