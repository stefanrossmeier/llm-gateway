"""Pure live-test authorization/budget checks; no key loading or network access."""

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import Field, StrictInt, model_validator

from llm_gateway.config.loader import canonical_json
from llm_gateway.contracts import InferenceRequest, Usd, usd_string
from llm_gateway.contracts.models import Alias, Contract, Identifier
from llm_gateway.providers.preparation import PreparationError, prepare
from llm_gateway.providers.pricing import PricingError, reserve_usd
from llm_gateway.storage.buckets import bind_bucket
from llm_gateway.storage.sqlite import StoreError, verify_private_ancestors


class LiveCase(Contract):
    case_id: Identifier
    request: InferenceRequest


class LivePlan(Contract):
    schema_version: Literal[1] = 1
    group: Literal["free", "paid"]
    project_id: Alias
    principal_id: Alias
    run_id: Identifier
    max_calls: StrictInt = Field(ge=1, le=20)
    total_ceiling_usd: Usd
    max_concurrency: StrictInt = Field(default=1, ge=1, le=2)
    cases: list[LiveCase] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def bounded_cases(self):
        if len(self.cases) > self.max_calls:
            raise ValueError("live_plan_call_limit")
        for values in ([c.case_id for c in self.cases], [c.request.request_id for c in self.cases]):
            if len(set(values)) != len(values):
                raise ValueError("live_plan_duplicate_identity")
        if (self.group == "free") != (self.total_ceiling_usd == 0):
            raise ValueError("live_plan_group_ceiling_conflict")
        if self.max_concurrency == 2 and (self.group != "paid" or len(self.cases) != 2):
            raise ValueError("live_parallel_smoke_requires_two_paid_cases")
        return self

    @property
    def digest(self):
        data = self.model_dump(mode="json")
        if self.max_concurrency == 1:
            # v1 plans predating this optional field retain their exact approval
            # identity. A concurrent pair has a distinct, explicit digest.
            data.pop("max_concurrency")
        return hashlib.sha256(canonical_json(data).encode()).hexdigest()


class PaidApproval(Contract):
    """Operator-owned approval record; presence of an API key is never approval."""

    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    total_ceiling_usd: Usd
    approved_by: str = Field(min_length=1, max_length=128)
    approved_at: datetime
    expires_at: datetime
    implementation_and_documentation_complete: Literal[True]


class PreflightError(ValueError):
    pass


def preflight(plan, snapshot, *, enabled=False, approval=None, now=None):
    """Validate every case before any authenticated live test can dispatch.

    Paid execution is additionally disabled by pytest until the human approves
    the final concrete plan. This function validates records; it cannot infer
    human authorization or create one from environment/provider credentials.
    """
    if not enabled:
        raise PreflightError("live_group_explicit_enablement_required")
    config = snapshot.config
    if config.mode != "live" or config.project_id != plan.project_id:
        raise PreflightError("live_project_identity_mismatch")
    if config.evidence_policy.cache_enabled:
        raise PreflightError("live_acceptance_must_not_use_result_cache")
    principal = config.access.principals.get(plan.principal_id)
    if (
        principal is None
        or principal.run_id != plan.run_id
        or principal.phase is not None
        or set(principal.permissions) != {"infer", "receipts:own"}
    ):
        raise PreflightError("live_run_bound_scoped_principal_required")
    now = now or datetime.now(UTC)
    if plan.group == "paid":
        if approval is None:
            raise PreflightError("paid_owner_approval_required")
        if (
            approval.approved_at.tzinfo is None
            or approval.expires_at.tzinfo is None
            or not approval.approved_at <= now < approval.expires_at
            or (approval.expires_at - approval.approved_at).total_seconds() > 86400
            or approval.plan_digest != plan.digest
            or approval.config_digest != snapshot.digest
            or approval.total_ceiling_usd != plan.total_ceiling_usd
        ):
            raise PreflightError("paid_approval_identity_or_expiry_invalid")
    with localcontext() as context:
        context.prec = 50
        total = Decimal("0")
        scope_reservations = {}
        routes = set()
        for case in plan.cases:
            request = case.request
            alias = config.aliases.get(request.alias)
            if (
                alias is None
                or request.alias not in principal.aliases
                or request.operation not in principal.operations
                or request.operation != alias.operation
                or request.context.run_id != plan.run_id
                or request.limits.max_attempts != 1
                or request.limits.max_usd is None
            ):
                raise PreflightError("live_case_authority_or_limit_invalid")
            policies = [config.budgets[name] for name in alias.budget_policy_refs]
            routes.add((config.providers[alias.provider_ref].kind, request.operation))
            if (
                plan.max_concurrency == 2
                and request.operation == "chat"
                and (request.input.response_format != "json_schema")
            ):
                raise PreflightError("parallel_smoke_requires_structured_router_chat")
            periods = {policy.period for policy in policies}
            if not {"call", "run"}.issubset(periods):
                raise PreflightError("live_persistent_call_and_run_budgets_required")
            if any(p.cap_usd > plan.total_ceiling_usd for p in policies):
                raise PreflightError("live_ledger_cap_exceeds_approval")
            if plan.group == "free":
                if (
                    request.operation != "chat"
                    or not alias.pricing
                    or not alias.pricing.free_only
                    or config.providers[alias.provider_ref].kind != "openrouter"
                    or alias.upstream_model != "openrouter/free"
                    or alias.operation_policy_ref is not None
                    or alias.attempt_policy.max_attempts != 1
                    or alias.attempt_policy.fallback_routes
                    or request.limits.max_usd != 0
                    or alias.limits.max_tool_calls not in {None, 0}
                    or any(p.cap_usd != 0 for p in policies)
                ):
                    raise PreflightError("live_free_zero_cost_boundary_required")
            elif not alias.pricing or alias.pricing.free_only or request.limits.max_usd <= 0:
                raise PreflightError("paid_case_explicit_bounded_ceiling_required")
            if plan.group == "paid":
                try:
                    prepared = prepare(
                        alias,
                        config.providers[alias.provider_ref],
                        request.input,
                        operation_policy=config.operation_policies.get(alias.operation_policy_ref),
                        caller_limits=request.limits,
                    )
                    reserve = reserve_usd(
                        alias, prepared.input_token_bound, prepared.max_output_tokens
                    )
                    if reserve > request.limits.max_usd:
                        raise PreflightError("paid_case_reservation_exceeds_ceiling")
                    for name in alias.budget_policy_refs:
                        bucket = bind_bucket(
                            name,
                            config.budgets[name],
                            project_id=config.project_id,
                            principal_id=plan.principal_id,
                            request_id=request.request_id,
                            provider_ref=alias.provider_ref,
                            alias=request.alias,
                            context=request.context,
                            timezone=config.timezone,
                            now=now,
                        )
                        previous = scope_reservations.get(bucket.bucket_id, Decimal("0"))
                        amount = previous + reserve
                        if amount > bucket.cap_usd:
                            raise PreflightError("paid_plan_reservations_exceed_server_cap")
                        scope_reservations[bucket.bucket_id] = amount
                except (PreparationError, PricingError, StoreError) as error:
                    raise PreflightError(str(error)) from None
            total += request.limits.max_usd
        if total > plan.total_ceiling_usd:
            raise PreflightError("live_case_ceilings_exceed_total_approval")
        if plan.max_concurrency == 2 and (
            plan.group != "paid"
            or len(plan.cases) != 2
            or routes != {("openai", "responses"), ("openrouter", "chat")}
        ):
            raise PreflightError("parallel_smoke_requires_distinct_paid_provider_routes")
    return {
        "plan_digest": plan.digest,
        "config_digest": snapshot.digest,
        "max_calls": plan.max_calls,
        "max_concurrency": plan.max_concurrency,
        "total_ceiling_usd": usd_string(plan.total_ceiling_usd),
    }


def _load_private(path, model):
    if not path.is_absolute():
        raise PreflightError("live_plan_private_absolute_file_required")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {os.getuid(), 0}
            or info.st_mode & 0o077
            or info.st_size > 65536
        ):
            raise PreflightError("live_plan_private_file_required")
        with os.fdopen(fd, "rb", closefd=False) as file:
            raw = file.read(65537)
        if len(raw) > 65536:
            raise PreflightError("live_plan_size_limit")
        return model.model_validate(json.loads(raw))
    except (OSError, ValueError):
        raise PreflightError("live_plan_unavailable_or_invalid") from None
    finally:
        if fd is not None:
            os.close(fd)


def load_plan(path):
    """Explicit private file; no path discovery or remote configuration."""
    return _load_private(path, LivePlan)


def load_approval(path):
    """Read an operator's private record; this never grants human authorization."""
    if not path.is_absolute():
        raise PreflightError("paid_approval_private_absolute_file_required")
    try:
        verify_private_ancestors(path.parent)
    except StoreError:
        raise PreflightError("paid_approval_private_ancestors_required") from None
    return _load_private(path, PaidApproval)
