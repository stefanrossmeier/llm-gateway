"""Synthetic approvals exercise the executor using only mocked inference."""

import importlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_config import private_file
from test_live_preflight import setup

from llm_gateway.contracts import CallerLimits
from llm_gateway.providers.preparation import prepare
from llm_gateway.providers.pricing import reserve_usd
from llm_gateway.testing.preflight import PaidApproval, PreflightError, load_approval, preflight
from llm_gateway.testing.runner import run_plan

pytestmark = pytest.mark.offline
pytest_plugins = ["test_live_runner"]


def approval_for(plan, snapshot, *, now=None):
    now = now or datetime.now(UTC)
    return PaidApproval(
        plan_digest=plan.digest,
        config_digest=snapshot.digest,
        total_ceiling_usd=plan.total_ceiling_usd,
        approved_by="synthetic offline test fixture; no human spending authorization",
        approved_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(hours=1),
        implementation_and_documentation_complete=True,
    )


class Forbidden:
    def __getattr__(self, name):
        raise AssertionError("failed preconditions must precede every gateway call")


def test_paid_flag_and_valid_record_are_independently_required(tmp_path):
    plan, snapshot = setup(tmp_path, paid=True)
    approval = approval_for(plan, snapshot)
    for kwargs, reason in [
        ({"enabled": True, "approval": approval}, "final_owner_approval"),
        ({"paid_enabled": True, "approval": approval}, "enablement_required"),
        ({"enabled": True, "paid_enabled": True}, "owner_approval_required"),
        (
            {
                "enabled": True,
                "paid_enabled": True,
                "approval": approval.model_copy(update={"config_digest": "0" * 64}),
            },
            "identity_or_expiry_invalid",
        ),
    ]:
        report = run_plan(plan, snapshot, Forbidden(), **kwargs)
        assert reason in report["precondition"]
        assert report["inference_posts_attempted"] == 0


@pytest.mark.parametrize("live_double", [True], indirect=True)
def test_paid_executor_and_rerun_preserve_bound_receipts_with_inference_double(live_double):
    plan, snapshot, client, engine, posts, _ = live_double
    approval = approval_for(plan, snapshot)
    report = run_plan(plan, snapshot, client, enabled=True, paid_enabled=True, approval=approval)
    assert report["status"] == "passed_execution"
    assert report["usage_after"]["attempts"] == 1
    assert report["cases"][0]["receipts"][0]["measured_usd"] == "0"
    assert posts[0].headers["X-Gateway-Expected-Config-Digest"] == snapshot.digest
    again = run_plan(plan, snapshot, client, enabled=True, paid_enabled=True, approval=approval)
    assert again["status"] == "recovered_existing"
    assert len(posts) == 2 and len(engine.provider.calls) == 1


@pytest.mark.parametrize("live_double", [True], indirect=True)
def test_approval_expiring_during_checkpoint_cannot_submit(live_double, monkeypatch):
    plan, snapshot, client, engine, posts, _ = live_double
    now = datetime.now(UTC)
    approval = approval_for(plan, snapshot, now=now)
    current = [now]

    class Clock:
        @staticmethod
        def now(tz):
            return current[0]

    module = importlib.import_module("llm_gateway.testing.preflight")
    monkeypatch.setattr(module, "datetime", Clock)

    def checkpoint(report):
        if report["inference_posts_attempted"]:
            current[0] = approval.expires_at + timedelta(seconds=1)

    report = run_plan(
        plan,
        snapshot,
        client,
        enabled=True,
        paid_enabled=True,
        approval=approval,
        report_sink=checkpoint,
    )
    assert report["status"] == "unrun_precondition"
    assert report["inference_posts_attempted"] == 0
    assert report["cases"][0]["status"] == "unrun_approval_expired_or_invalid"
    assert not posts and not engine.provider.calls


@pytest.mark.parametrize("live_double", [True], indirect=True)
def test_approval_expiry_after_first_case_stops_second(live_double, monkeypatch):
    plan, snapshot, client, engine, posts, _ = live_double
    second = plan.cases[0].model_copy(
        update={
            "case_id": "second",
            "request": plan.cases[0].request.model_copy(
                update={
                    "request_id": "second",
                    "limits": CallerLimits(max_attempts=1, max_usd="0.05"),
                }
            ),
        }
    )
    first = plan.cases[0].model_copy(
        update={
            "request": plan.cases[0].request.model_copy(
                update={"limits": CallerLimits(max_attempts=1, max_usd="0.05")}
            )
        }
    )
    plan = plan.model_copy(update={"max_calls": 2, "cases": [first, second]})
    now = datetime.now(UTC)
    approval = approval_for(plan, snapshot, now=now)
    current = [now]

    class Clock:
        @staticmethod
        def now(tz):
            return current[0]

    monkeypatch.setattr(importlib.import_module("llm_gateway.testing.preflight"), "datetime", Clock)

    def checkpoint(report):
        if report["cases"][0]["status"] == "passed_execution":
            current[0] = approval.expires_at + timedelta(seconds=1)

    report = run_plan(
        plan,
        snapshot,
        client,
        enabled=True,
        paid_enabled=True,
        approval=approval,
        report_sink=checkpoint,
    )
    assert report["status"] == "failed_precondition_after_submission"
    assert report["inference_posts_attempted"] == 1
    assert report["cases"][1]["status"] == "unrun"
    assert len(posts) == 1 and len(engine.provider.calls) == 1


def test_prepared_reservation_and_combined_server_caps_are_checked_before_gateway(tmp_path):
    plan, snapshot = setup(tmp_path, paid=True)
    request = plan.cases[0].request
    tiny = plan.model_copy(
        update={
            "cases": [
                plan.cases[0].model_copy(
                    update={
                        "request": request.model_copy(
                            update={"limits": CallerLimits(max_attempts=1, max_usd="0.000001")}
                        )
                    }
                )
            ]
        }
    )
    report = run_plan(
        tiny,
        snapshot,
        Forbidden(),
        enabled=True,
        paid_enabled=True,
        approval=approval_for(tiny, snapshot),
    )
    assert report["precondition"] == "paid_case_reservation_exceeds_ceiling"
    config = snapshot.config.model_copy(deep=True)
    alias = config.aliases[request.alias]
    prepared = prepare(alias, config.providers[alias.provider_ref], request.input)
    reserve = reserve_usd(alias, prepared.input_token_bound, prepared.max_output_tokens)
    config.budgets["run"] = config.budgets["run"].model_copy(
        update={"cap_usd": reserve * Decimal("1.5")}
    )
    first = plan.cases[0].model_copy(
        update={
            "request": request.model_copy(
                update={"limits": CallerLimits(max_attempts=1, max_usd="0.05")}
            )
        }
    )
    second = first.model_copy(
        update={
            "case_id": "second",
            "request": first.request.model_copy(update={"request_id": "second"}),
        }
    )
    plan = plan.model_copy(update={"max_calls": 2, "cases": [first, second]})
    with pytest.raises(PreflightError, match="reservations_exceed_server_cap"):
        preflight(
            plan,
            SimpleNamespace(config=config, digest=snapshot.digest),
            enabled=True,
            approval=approval_for(plan, snapshot),
        )


@pytest.mark.parametrize("kind", ["relative", "public", "symlink", "fifo", "oversize", "invalid"])
def test_approval_reader_rejects_unsafe_records_without_blocking(tmp_path, kind):
    plan, snapshot = setup(tmp_path, paid=True)
    path = private_file(tmp_path, "approval.json", approval_for(plan, snapshot).model_dump_json())
    if kind == "relative":
        path = Path("approval.json")
    elif kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        target = path
        path = tmp_path / "link"
        path.symlink_to(target)
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif kind == "oversize":
        path.write_bytes(b" " * 65537)
    else:
        path.write_text("{}")
    with pytest.raises(PreflightError):
        load_approval(path)


def test_private_approval_roundtrip_and_cli_separate_group_guards(tmp_path, monkeypatch):
    from llm_gateway.testing import run

    plan, snapshot = setup(tmp_path, paid=True)
    approval = approval_for(plan, snapshot)
    path = private_file(tmp_path, "approval.json", approval.model_dump_json())
    assert load_approval(path) == approval
    plan_path = private_file(tmp_path, "plan.json", plan.model_dump_json())
    monkeypatch.setattr(run, "GatewayClient", lambda *a, **k: Forbidden())
    args = [
        "--plan",
        str(plan_path),
        "--project-config",
        str(tmp_path / "project.yaml"),
        "--gateway-origin",
        "http://gateway.test",
        "--report",
        str(tmp_path / "report"),
        "--paid-approval",
        str(path),
    ]
    assert run.main(args + ["--enable-free"]) == 2
    assert not (tmp_path / "report").exists()
    assert json.loads(path.read_text())["approved_by"].startswith("synthetic offline")


@pytest.mark.parametrize("live_double", [True], indirect=True)
def test_paid_cli_uses_only_explicit_record_and_gateway_client(live_double, tmp_path, monkeypatch):
    from llm_gateway.testing import run

    plan, snapshot, client, engine, posts, _ = live_double
    plan_path = private_file(tmp_path, "plan.json", plan.model_dump_json())
    approval_path = private_file(
        tmp_path, "synthetic-approval.json", approval_for(plan, snapshot).model_dump_json()
    )
    monkeypatch.setattr(run, "GatewayClient", lambda *a, **k: client)
    report = tmp_path / "paid-double-report.json"
    assert (
        run.main(
            [
                "--plan",
                str(plan_path),
                "--project-config",
                str(tmp_path / "project.yaml"),
                "--gateway-origin",
                "http://gateway.test",
                "--report",
                str(report),
                "--enable-paid",
                "--paid-approval",
                str(approval_path),
            ]
        )
        == 0
    )
    assert json.loads(report.read_text())["status"] == "passed_execution"
    assert len(posts) == 1 and len(engine.provider.calls) == 1


def test_public_approval_parent_and_invalid_preparation_fail_without_gateway(tmp_path):
    plan, snapshot = setup(tmp_path, paid=True)
    path = private_file(tmp_path, "approval.json", approval_for(plan, snapshot).model_dump_json())
    tmp_path.chmod(0o755)
    try:
        with pytest.raises(PreflightError, match="private_ancestors"):
            load_approval(path)
    finally:
        tmp_path.chmod(0o700)
    bad = plan.model_copy(
        update={
            "cases": [
                plan.cases[0].model_copy(
                    update={
                        "request": plan.cases[0].request.model_copy(
                            update={
                                "limits": CallerLimits(
                                    max_attempts=1, max_usd="0.1", max_output_tokens=100000
                                )
                            }
                        )
                    }
                )
            ]
        }
    )
    report = run_plan(
        bad,
        snapshot,
        Forbidden(),
        enabled=True,
        paid_enabled=True,
        approval=approval_for(bad, snapshot),
    )
    assert report["status"] == "unrun_precondition"
    assert report["inference_posts_attempted"] == 0
