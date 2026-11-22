"""Paid pytest wiring is verified with doubles; no spending authorization."""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_config import private_file
from test_live_preflight import setup
from test_paid_executor import approval_for

from llm_gateway.testing import run

pytestmark = pytest.mark.offline
pytest_plugins = ["test_live_runner"]
ROOT = Path(__file__).resolve().parents[2]


def module():
    return runpy.run_path(str(ROOT / "tests/live/test_paid.py"))


def request_for(**values):
    return SimpleNamespace(config=SimpleNamespace(getoption=lambda key: values.get(key)))


@pytest.mark.parametrize("options", [{}, {"--run-live-paid": True}, {"--live-paid-approval": "/x"}])
def test_missing_explicit_paid_enablement_skips_before_reading_files(options):
    with pytest.raises(pytest.skip.Exception):
        module()["paid_run"].__wrapped__(request_for(**options))


def test_missing_paid_setup_skips_before_executor(monkeypatch):
    monkeypatch.setattr(run, "main", lambda args: pytest.fail("executor must not run"))
    with pytest.raises(pytest.skip.Exception, match="Explicit paid"):
        module()["paid_run"].__wrapped__(
            request_for(**{"--run-live-paid": True, "--live-paid-approval": "/x"})
        )


@pytest.mark.parametrize("kind", ["free", "invalid", "failed", "existing_report"])
def test_invalid_or_failed_paid_entry_never_accepts_stale_success(tmp_path, monkeypatch, kind):
    plan, _ = setup(tmp_path, paid=kind != "free")
    plan_path = private_file(tmp_path, "plan.json", plan.model_dump_json())
    report = private_file(tmp_path, "report.json", '{"status":"passed_execution"}')
    if kind == "invalid":
        plan_path.write_text("private-invalid-canary")
    calls = []

    def fail(args):
        calls.append(args)
        return 2

    monkeypatch.setattr(run, "main", fail)
    with pytest.raises(pytest.fail.Exception, match="failed safely") as error:
        module()["paid_run"].__wrapped__(
            request_for(
                **{
                    "--run-live-paid": True,
                    "--live-paid-approval": "/synthetic/approval",
                    "--live-plan": str(plan_path),
                    "--live-project-config": "/synthetic/config",
                    "--live-gateway-origin": "http://gateway.test",
                    "--live-report": str(report),
                }
            )
        )
    assert "private-invalid-canary" not in str(error.value)
    assert bool(calls) == (kind in {"failed", "existing_report"})


@pytest.mark.parametrize("live_double", [True], indirect=True)
def test_paid_pytest_uses_shared_executor_and_stable_recovery(live_double, tmp_path, monkeypatch):
    plan, snapshot, client, engine, posts, _ = live_double
    plan_path = private_file(tmp_path, "plan.json", plan.model_dump_json())
    approval = private_file(
        tmp_path, "approval.json", approval_for(plan, snapshot).model_dump_json()
    )
    token = private_file(tmp_path, "client.token", "t" * 48)
    monkeypatch.setattr(run, "GatewayClient", lambda *args, **kwargs: client)
    options = {
        "--run-live-paid": True,
        "--live-paid-approval": str(approval),
        "--live-plan": str(plan_path),
        "--live-project-config": str(tmp_path / "project.yaml"),
        "--live-gateway-origin": "http://gateway.test",
        "--live-gateway-token-file": str(token),
        "--live-report": str(tmp_path / "first.json"),
    }
    loaded = module()
    first = loaded["paid_run"].__wrapped__(request_for(**options))
    loaded["test_approved_paid_plan"](first)
    # The real client context has closed; the ASGI service remains available.
    # Replay classification is tested independently without another POST here.
    recovered = (plan, first[1] | {"status": "recovered_existing"})
    with pytest.raises(pytest.skip.Exception, match="Existing receipts"):
        loaded["test_approved_paid_plan"](recovered)
    assert len(posts) == len(engine.provider.calls) == 1
    assert json.loads((tmp_path / "first.json").read_text()) == first[1]
