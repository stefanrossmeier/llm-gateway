from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_config import free_config, write_config
from test_ledger import request

from llm_gateway.config import load_project_config
from llm_gateway.contracts import CallerLimits
from llm_gateway.testing.preflight import (
    LivePlan,
    PaidApproval,
    PreflightError,
    load_plan,
    preflight,
)

pytestmark = pytest.mark.offline


def setup(tmp_path, paid=False):
    config = free_config()
    config["access"] = {
        "principals": {
            "test": {
                "token_file": "/unused",
                "run_id": "run-one",
                "aliases": ["summary"],
                "operations": ["chat"],
                "permissions": ["infer", "receipts:own"],
            }
        }
    }
    cap = "0.1" if paid else "0"
    config["budgets"] = {
        "call": {"scope": "project", "period": "call", "cap_usd": cap},
        "run": {"scope": "run", "period": "run", "cap_usd": cap},
    }
    config["aliases"]["summary"]["budget_policy_refs"] = ["call", "run"]
    if paid:
        config["aliases"]["summary"].update(
            upstream_model="synthetic/model",
            pricing={
                "revision": "fixture",
                "bound_source": "fixture",
                "input_bound": "utf8_bytes_with_framing",
                "framing_tokens_per_item": 64,
                "input_usd_per_token": "0.000001",
                "output_usd_per_token": "0.000001",
            },
        )
    snapshot = load_project_config(write_config(tmp_path, config), check_credentials=False)
    req = request().model_copy(update={"limits": CallerLimits(max_attempts=1, max_usd=cap)})
    plan = LivePlan(
        group="paid" if paid else "free",
        project_id="project-a",
        principal_id="test",
        run_id="run-one",
        max_calls=1,
        total_ceiling_usd=cap,
        cases=[{"case_id": "text", "request": req}],
    )
    return plan, snapshot


def test_free_preflight_needs_enablement_and_zero_ledger_authority(tmp_path):
    plan, snapshot = setup(tmp_path)
    with pytest.raises(PreflightError, match="enablement"):
        preflight(plan, snapshot)
    assert preflight(plan, snapshot, enabled=True)["total_ceiling_usd"] == "0"
    assert not snapshot.config.providers["router"].credential.file_path


@pytest.mark.parametrize(
    "change",
    ["model", "provider", "retry", "fallback", "tool", "policy", "price", "cap", "run", "admin"],
)
def test_free_gate_denies_paid_expansions_and_authority_changes(tmp_path, change):
    plan, snapshot = setup(tmp_path)
    config = snapshot.config.model_copy(deep=True)
    alias = config.aliases["summary"]
    if change == "model":
        alias = alias.model_copy(update={"upstream_model": "paid/model"})
    elif change == "provider":
        config.providers["router"] = config.providers["router"].model_copy(
            update={"kind": "openai"}
        )
    elif change == "retry":
        alias = alias.model_copy(
            update={"attempt_policy": alias.attempt_policy.model_copy(update={"max_attempts": 2})}
        )
    elif change == "fallback":
        alias = alias.model_copy(
            update={
                "attempt_policy": alias.attempt_policy.model_copy(
                    update={"fallback_routes": [object()]}
                )
            }
        )
    elif change == "tool":
        alias = alias.model_copy(
            update={"limits": alias.limits.model_copy(update={"max_tool_calls": 1})}
        )
    elif change == "policy":
        alias = alias.model_copy(update={"operation_policy_ref": "hosted-tool"})
    elif change == "price":
        alias = alias.model_copy(
            update={"pricing": alias.pricing.model_copy(update={"free_only": False})}
        )
    elif change == "cap":
        config.budgets["call"] = config.budgets["call"].model_copy(update={"cap_usd": 1})
    elif change == "run":
        config.access.principals["test"] = config.access.principals["test"].model_copy(
            update={"run_id": None}
        )
    elif change == "admin":
        config.access.principals["test"].permissions.append("admin:reload")
    config.aliases["summary"] = alias
    with pytest.raises(PreflightError):
        preflight(plan, SimpleNamespace(config=config, digest=snapshot.digest), enabled=True)


def test_paid_gate_binds_explicit_approval_config_time_and_total(tmp_path):
    plan, snapshot = setup(tmp_path, paid=True)
    with pytest.raises(PreflightError, match="owner_approval"):
        preflight(plan, snapshot, enabled=True)
    now = datetime.now(UTC)
    approval = PaidApproval(
        plan_digest=plan.digest,
        config_digest=snapshot.digest,
        total_ceiling_usd="0.1",
        approved_by="synthetic offline approval fixture",
        approved_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(hours=1),
        implementation_and_documentation_complete=True,
    )
    assert preflight(plan, snapshot, enabled=True, approval=approval, now=now)
    for changed in [
        approval.model_copy(update={"config_digest": "0" * 64}),
        approval.model_copy(update={"plan_digest": "0" * 64}),
        approval.model_copy(update={"expires_at": now - timedelta(seconds=1)}),
        approval.model_copy(update={"total_ceiling_usd": 1}),
    ]:
        with pytest.raises(PreflightError, match="identity_or_expiry"):
            preflight(plan, snapshot, enabled=True, approval=changed, now=now)


def test_plan_file_is_private_explicit_bounded_and_multiline(tmp_path):
    plan, _ = setup(tmp_path)
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json(indent=2))
    path.chmod(0o600)
    assert load_plan(path) == plan
    path.chmod(0o644)
    with pytest.raises(PreflightError):
        load_plan(path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(PreflightError):
        load_plan(link)


def test_live_acceptance_cannot_substitute_cached_output_for_inference(tmp_path):
    from llm_gateway.config.models import EvidencePolicy

    plan, snapshot = setup(tmp_path)
    config = snapshot.config.model_copy(
        update={
            "evidence_policy": EvidencePolicy(
                retain_results=True, result_ttl_seconds=60, cache_enabled=True
            )
        }
    )
    with pytest.raises(PreflightError, match="must_not_use_result_cache"):
        preflight(plan, SimpleNamespace(config=config, digest=snapshot.digest), enabled=True)
