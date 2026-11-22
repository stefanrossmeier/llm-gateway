"""Malformed plans and unbounded proposals fail without keys, HTTP or approval."""

import json
import os
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_config import private_file, write_config
from test_live_preflight import setup
from test_paid_executor import approval_for
from test_paid_proposal import project, proposal

from llm_gateway.config import load_project_config
from llm_gateway.contracts import CallerLimits
from llm_gateway.contracts.models import XSearchInput
from llm_gateway.testing.preflight import LivePlan, PreflightError, load_plan, preflight
from llm_gateway.testing.proposal import _input

pytestmark = pytest.mark.offline


@pytest.mark.parametrize(
    "kind", ["call_count", "duplicate_case", "duplicate_request", "free_positive", "paid_zero"]
)
def test_plan_shape_never_creates_ambiguous_or_unbounded_live_identity(tmp_path, kind):
    plan, _ = setup(tmp_path)
    data = plan.model_dump(mode="json")
    if kind in {"free_positive", "paid_zero"}:
        data.update(
            group="paid" if kind == "paid_zero" else "free",
            total_ceiling_usd="0" if kind == "paid_zero" else "0.1",
        )
    else:
        second = json.loads(json.dumps(data["cases"][0]))
        if kind != "duplicate_case":
            second["case_id"] = "second"
        if kind != "duplicate_request":
            second["request"]["request_id"] = "second"
        data["cases"].append(second)
        data["max_calls"] = 1 if kind == "call_count" else 2
    with pytest.raises(ValidationError):
        LivePlan.model_validate(data)


def test_live_project_and_run_scope_mismatch_fail_before_inference(tmp_path):
    plan, snapshot = setup(tmp_path)
    for config in [
        snapshot.config.model_copy(update={"mode": "fake"}),
        snapshot.config.model_copy(update={"project_id": "other"}),
    ]:
        with pytest.raises(PreflightError, match="project_identity"):
            preflight(plan, SimpleNamespace(config=config, digest=snapshot.digest), enabled=True)
    request = plan.cases[0].request
    for changed in [
        request.model_copy(update={"alias": "missing"}),
        request.model_copy(
            update={"context": request.context.model_copy(update={"run_id": "other"})}
        ),
    ]:
        altered = plan.model_copy(
            update={"cases": [plan.cases[0].model_copy(update={"request": changed})]}
        )
        with pytest.raises(PreflightError, match="authority_or_limit"):
            preflight(altered, snapshot, enabled=True)


def test_free_acceptance_still_requires_both_persistent_budget_periods(tmp_path):
    plan, snapshot = setup(tmp_path)
    data = snapshot.config.model_dump(mode="json")
    data["aliases"]["summary"]["budget_policy_refs"] = ["call"]
    changed = load_project_config(write_config(tmp_path, data), check_credentials=False)
    with pytest.raises(PreflightError, match="call_and_run_budgets"):
        preflight(plan, changed, enabled=True)


def test_paid_group_cannot_use_free_pricing_or_exceed_total_case_ceilings(tmp_path):
    free, free_snapshot = setup(tmp_path)
    paid = free.model_copy(
        update={
            "group": "paid",
            "total_ceiling_usd": Decimal("0.1"),
            "cases": [
                free.cases[0].model_copy(
                    update={
                        "request": free.cases[0].request.model_copy(
                            update={"limits": CallerLimits(max_attempts=1, max_usd="0.1")}
                        )
                    }
                )
            ],
        }
    )
    with pytest.raises(PreflightError, match="explicit_bounded_ceiling"):
        preflight(paid, free_snapshot, enabled=True, approval=approval_for(paid, free_snapshot))
    plan, snapshot = setup(tmp_path, paid=True)
    first = plan.cases[0].model_copy(
        update={
            "request": plan.cases[0].request.model_copy(
                update={"limits": CallerLimits(max_attempts=1, max_usd="0.06")}
            )
        }
    )
    second = first.model_copy(
        update={
            "case_id": "second",
            "request": first.request.model_copy(update={"request_id": "second"}),
        }
    )
    plan = plan.model_copy(update={"cases": [first, second], "max_calls": 2})
    with pytest.raises(PreflightError, match="ceilings_exceed_total"):
        preflight(plan, snapshot, enabled=True, approval=approval_for(plan, snapshot))


def test_private_plan_relative_path_and_growth_race_remain_bounded(tmp_path, monkeypatch):
    with pytest.raises(PreflightError):
        load_plan(Path("relative.json"))
    plan, _ = setup(tmp_path)
    path = private_file(tmp_path, "plan.json", plan.model_dump_json())
    original = os.fstat

    def changed(fd):
        info = original(fd)
        path.write_bytes(b" " * 65537)
        return info

    monkeypatch.setattr(os, "fstat", changed)
    with pytest.raises(PreflightError):
        load_plan(path)


@pytest.mark.parametrize(
    "change,role,reason",
    [
        ("free", "router_schema", "reviewed_paid_pricing"),
        ("capability", "router_schema", "required_capability"),
        ("profile", "direct_responses", "plain_profile"),
        ("cap", "router_schema", "not_admissible_within_caps"),
    ],
)
def test_proposal_cannot_invent_pricing_capability_profile_or_allowance(
    tmp_path, change, role, reason
):
    snapshot = project(tmp_path)
    data = snapshot.config.model_dump(mode="json")
    if change == "free":
        data["aliases"]["summary"].update(
            upstream_model="openrouter/free",
            pricing={
                "revision": "free",
                "free_only": True,
                "bound_source": "synthetic-free-policy",
            },
        )
    elif change == "capability":
        data["aliases"]["summary"]["capabilities"] = ["plain_text"]
    elif change == "profile":
        data["aliases"]["response"]["capabilities"].append("json_object")
        data["aliases"]["response"]["generation"]["response_format"] = "json_object"
    else:
        for policy in data["budgets"].values():
            policy["cap_usd"] = "0.001"
    changed = load_project_config(write_config(tmp_path, data), check_credentials=False)
    result = proposal(changed, {role: "response" if role == "direct_responses" else "summary"})
    assert result["plan"] is None and reason in result["blocked_cases"][0]["reason"]


def test_proposal_requires_scoped_principal_and_does_not_trust_invalid_in_memory_policy(tmp_path):
    snapshot = project(tmp_path)
    config = snapshot.config.model_copy(deep=True)
    config.access.principals["test"].permissions.append("usage:project")
    with pytest.raises(PreflightError, match="run_bound_scoped"):
        proposal(SimpleNamespace(config=config))
    # Defensive embedding boundary: this invalid in-memory profile cannot be
    # loaded as paid YAML, and must not cause a proposal to invent run authority.
    config = snapshot.config.model_copy(deep=True)
    config.aliases["summary"] = config.aliases["summary"].model_copy(
        update={"budget_policy_refs": ["call"]}
    )
    result = proposal(SimpleNamespace(config=config), {"router_schema": "summary"})
    assert result["plan"] is None and "call_and_run_caps" in result["blocked_cases"][0]["reason"]


def test_proposal_total_covers_distinct_alias_budget_buckets_too(tmp_path):
    snapshot = project(tmp_path)
    data = snapshot.config.model_dump(mode="json")
    data["budgets"]["run"]["scope"] = "alias"
    for policy in data["budgets"].values():
        policy["cap_usd"] = "0.02"
    changed = load_project_config(write_config(tmp_path, data), check_credentials=False)
    with pytest.raises(PreflightError, match="case_ceilings_exceed_total"):
        proposal(
            changed, {"direct_responses": "response", "router_schema": "summary"}, total="0.02"
        )


def test_native_x_proposal_form_is_scoped_but_still_not_executable(tmp_path):
    snapshot = project(tmp_path)
    config = snapshot.config
    data = _input("native_x", config, config.aliases["search"], date(2026, 11, 11))
    form = XSearchInput.model_validate(data)
    assert form.allowed_x_handles == ["public_example"]
    assert form.from_date == date(2026, 11, 10) and form.to_date == date(2026, 11, 11)
    with pytest.raises(PreflightError, match="explicit_scope_policy"):
        _input(
            "native_x",
            SimpleNamespace(operation_policies={}),
            config.aliases["search"],
            date(2026, 11, 11),
        )
    assert proposal(snapshot, {"native_x": "search"})["plan"] is None


@pytest.mark.parametrize("missing", ["plan", "principal"])
def test_installed_live_cli_missing_authority_exits_before_creating_report(tmp_path, missing):
    plan, _ = setup(tmp_path)
    if missing == "principal":
        plan = plan.model_copy(update={"principal_id": "missing"})
    plan_path = tmp_path / "missing.json"
    if missing == "principal":
        plan_path = private_file(tmp_path, "plan.json", plan.model_dump_json())
    report = tmp_path / "report.json"
    process = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "llm_gateway.testing.run",
            "--plan",
            str(plan_path),
            "--project-config",
            str(tmp_path / "project.yaml"),
            "--gateway-origin",
            "http://never.invalid",
            "--report",
            str(report),
            "--enable-free",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 2
    assert json.loads(process.stderr)["code"] == "live_run_failed_preserve_ids_and_ledger"
    assert not report.exists()
