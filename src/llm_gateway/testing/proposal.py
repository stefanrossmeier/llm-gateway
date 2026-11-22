"""Construct reviewable paid cases from project-owned aliases; never authorize/run."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from llm_gateway.contracts import InferenceRequest, add_usd, parse_usd, usd_string
from llm_gateway.providers.pricing import PricingError, reserve_usd
from llm_gateway.storage.buckets import bind_bucket
from llm_gateway.storage.sqlite import StoreError

from .preflight import LivePlan, PreflightError

ROLES = {
    "direct_responses": ("responses", "openai"),
    "router_schema": ("chat", "openrouter"),
    "function_responses": ("responses", None),
    "decisions": ("decisions", None),
    "embeddings": ("embeddings", None),
    "native_x": ("x_search", "openrouter"),
}
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _input(role, config, alias, today):
    if role == "router_schema":
        return {
            "kind": "chat",
            "messages": [{"role": "user", "content": "Return ok true."}],
            "response_format": "json_schema",
            "response_schema": SCHEMA,
        }
    if role in {"direct_responses", "function_responses"}:
        data = {"kind": "responses", "input": "Reply READY.", "store": False}
        if role == "function_responses":
            data.update(
                input="Call record_check with ok true.",
                tools=[{"type": "function", "name": "record_check", "parameters": SCHEMA}],
                tool_choice={"type": "function", "name": "record_check"},
                parallel_tool_calls=False,
            )
        return data
    if role == "decisions":
        return {
            "kind": "decisions",
            "state": {"public_check": True},
            "questions": {"ok": {"type": "noul", "instructions": "Is public_check true?"}},
        }
    if role == "embeddings":
        return {
            "kind": "embeddings",
            "input": ["Public synthetic embedding check."],
            **(
                {"dimensions": alias.limits.embedding_dimensions}
                if alias.limits.embedding_dimensions
                else {}
            ),
        }
    policy = config.operation_policies.get(alias.operation_policy_ref)
    if policy is None or policy.kind != "x_search":
        raise PreflightError("native_x_explicit_scope_policy_required")
    return {
        "kind": "x_search",
        "input": "Find a public post within the permitted scope.",
        "allowed_x_handles": [policy.allowed_x_handles[0]],
        "from_date": str(today - timedelta(days=1)),
        "to_date": str(today),
        "response_schema": {
            "type": "object",
            "properties": {"posts": {"type": "array"}},
            "required": ["posts"],
            "additionalProperties": False,
        },
    }


def propose_paid_plan(snapshot, *, principal_id, run_id, selections, total_ceiling_usd, today=None):
    """Return a proposal plus blocked cases; no approval records, secrets or HTTP.

    Ceiling uses the entire configured enforceable input-token bound, including
    transformed framing/state, rather than guessing token counts for short text.
    Actual admission still prepares/validates the request and checks every cap.
    """
    config = snapshot.config
    total_ceiling = parse_usd(total_ceiling_usd)
    if (
        config.mode != "live"
        or total_ceiling <= 0
        or not selections
        or set(selections) - ROLES.keys()
    ):
        raise PreflightError("paid_proposal_explicit_live_selection_required")
    principal = config.access.principals.get(principal_id)
    if (
        principal is None
        or principal.run_id != run_id
        or principal.phase is not None
        or set(principal.permissions) != {"infer", "receipts:own"}
        or config.evidence_policy.cache_enabled
    ):
        raise PreflightError("paid_proposal_run_bound_scoped_principal_required")
    today = today or datetime.now(UTC).date()
    cases, blocked, bounds = [], [], []
    scope_ceilings = {}
    for role in ROLES:
        if role not in selections:
            continue
        alias_name = selections[role]
        alias = config.aliases.get(alias_name)
        operation, kind = ROLES[role]
        if (
            alias is None
            or alias.operation != operation
            or alias_name not in principal.aliases
            or operation not in principal.operations
            or kind
            and config.providers[alias.provider_ref].kind != kind
        ):
            blocked.append({"case_id": role, "reason": "selected_alias_or_grant_mismatch"})
            continue
        try:
            if alias.pricing is None or alias.pricing.free_only:
                raise PreflightError("paid_proposal_reviewed_paid_pricing_required")
            required_cap = {
                "direct_responses": "plain_text",
                "router_schema": "json_schema",
                "function_responses": "function_calls",
                "native_x": "native_x_search",
            }.get(role)
            if required_cap and required_cap not in alias.capabilities:
                raise PreflightError("paid_proposal_required_capability_unavailable")
            if alias.generation.response_format not in {None, "text"} and role in {
                "direct_responses",
                "function_responses",
            }:
                raise PreflightError("paid_proposal_plain_profile_required_for_responses_cases")
            policies = [config.budgets[name] for name in alias.budget_policy_refs]
            if not {"call", "run"}.issubset({p.period for p in policies}):
                raise PreflightError("paid_proposal_persistent_call_and_run_caps_required")
            if any(p.cap_usd > total_ceiling for p in policies):
                raise PreflightError("paid_proposal_server_cap_exceeds_total")
            output_bound = (
                min(alias.limits.max_output_tokens, 256)
                if operation not in {"embeddings", "decisions"}
                and alias.limits.max_output_tokens is not None
                else None
            )
            ceiling = reserve_usd(
                alias,
                alias.limits.max_input_tokens,
                output_bound,
                provider_kind=config.providers[alias.provider_ref].kind,
            )
            if ceiling <= 0 or any(p.cap_usd < ceiling for p in policies):
                raise PreflightError("paid_proposal_case_not_admissible_within_caps")
            data = _input(role, config, alias, today)
            request = InferenceRequest.model_validate(
                {
                    "request_id": f"{run_id}:{role}",
                    "alias": alias_name,
                    "operation": operation,
                    "input": data,
                    "context": {"run_id": run_id, "test_group": "live_paid"},
                    "limits": {
                        "max_attempts": 1,
                        "max_usd": usd_string(ceiling),
                        **({"max_output_tokens": output_bound} if output_bound is not None else {}),
                        "timeout_seconds": alias.limits.request_timeout_seconds,
                    },
                }
            )
            buckets = [
                bind_bucket(
                    name,
                    config.budgets[name],
                    project_id=config.project_id,
                    principal_id=principal_id,
                    request_id=request.request_id,
                    provider_ref=alias.provider_ref,
                    alias=alias_name,
                    context=request.context,
                    timezone=config.timezone,
                    now=datetime.combine(today, datetime.min.time(), UTC),
                )
                for name in alias.budget_policy_refs
            ]
            for bucket in buckets:
                previous = scope_ceilings.get(bucket.bucket_id, (Decimal("0"), bucket.cap_usd))[0]
                scope_ceilings[bucket.bucket_id] = (add_usd(previous, ceiling), bucket.cap_usd)
            cases.append({"case_id": role, "request": request})
            bounds.append(
                {
                    "case_id": role,
                    "alias": alias_name,
                    "input_token_ceiling": alias.limits.max_input_tokens,
                    "output_token_ceiling": output_bound,
                    "tool_call_ceiling": alias.limits.max_tool_calls,
                    "max_attempts": 1,
                    "ceiling_usd": usd_string(ceiling),
                    "pricing_revision": alias.pricing.revision,
                }
            )
        except (PreflightError, PricingError, StoreError) as error:
            blocked.append({"case_id": role, "reason": str(error)})
    if any(amount > cap for amount, cap in scope_ceilings.values()):
        raise PreflightError("paid_proposal_combined_scope_ceilings_exceed_caps")
    requested = (
        add_usd(*(case["request"].limits.max_usd for case in cases)) if cases else Decimal("0")
    )
    if requested > total_ceiling:
        raise PreflightError("paid_proposal_case_ceilings_exceed_total")
    plan = (
        LivePlan(
            group="paid",
            project_id=config.project_id,
            principal_id=principal_id,
            run_id=run_id,
            max_calls=len(cases),
            total_ceiling_usd=usd_string(total_ceiling),
            cases=cases,
        )
        if cases
        else None
    )
    return {
        "status": "proposal_only_requires_final_human_approval",
        "plan": plan,
        "bounds": bounds,
        "blocked_cases": blocked,
        "sum_case_ceilings_usd": usd_string(requested),
        "execution_authorized": False,
    }


def propose_paid_smoke_pair(
    snapshot, *, principal_id, run_id, direct_alias, router_alias, total_ceiling_usd, today=None
):
    """A reviewable two-provider pair; no credentials, approval or execution."""
    result = propose_paid_plan(
        snapshot,
        principal_id=principal_id,
        run_id=run_id,
        selections={"direct_responses": direct_alias, "router_schema": router_alias},
        total_ceiling_usd=total_ceiling_usd,
        today=today,
    )
    plan = result["plan"]
    if plan is None or len(plan.cases) != 2:
        result["plan"] = None
        result["blocked_cases"].append(
            {
                "case_id": "concurrent_provider_pair",
                "reason": "parallel_smoke_requires_both_routes",
            }
        )
    else:
        result["plan"] = LivePlan.model_validate(
            plan.model_dump(mode="json") | {"max_concurrency": 2}
        )
    return result
