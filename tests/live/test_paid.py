"""Final approved acceptance only; project-owned plan selects bounded cases."""

import json
from pathlib import Path

import pytest

from llm_gateway.testing import run
from llm_gateway.testing.preflight import PreflightError, load_plan

pytestmark = pytest.mark.live_paid


@pytest.fixture(scope="session")
def paid_run(request):
    # Session fixtures run before the function autouse guard. Gate here too.
    if not request.config.getoption("--run-live-paid") or not request.config.getoption(
        "--live-paid-approval"
    ):
        pytest.skip("Paid execution requires final human approval, private record and flag")
    names = {
        "--plan": "--live-plan",
        "--project-config": "--live-project-config",
        "--gateway-origin": "--live-gateway-origin",
        "--report": "--live-report",
        "--paid-approval": "--live-paid-approval",
    }
    values = {name: request.config.getoption(option) for name, option in names.items()}
    if not all(values.values()):
        pytest.skip("Explicit paid plan/config/gateway/new private report required")
    failure = None
    try:
        plan = load_plan(Path(values["--plan"]))
        if plan.group != "paid":
            raise PreflightError("paid_pytest_group_cannot_select_free_plan")
        args = [item for name, value in values.items() for item in (name, str(value))]
        args.append("--enable-paid")
        token = request.config.getoption("--live-gateway-token-file")
        if token:
            args.extend(["--gateway-token-file", str(token)])
        # The same executor owns config/approval checks, stable IDs, checkpoint
        # creation and cost evidence. A failed/existing report is never accepted.
        if run.main(args) != 0:
            raise PreflightError("paid_acceptance_failed_preserve_private_report")
        report = json.loads(Path(values["--report"]).read_text())
        return plan, report
    except (PreflightError, OSError, ValueError):
        failure = "Paid setup/acceptance failed safely; inspect the private report and ledger"
    # Do not expose private validation context through pytest traceback rendering.
    pytest.fail(failure, pytrace=False)


def test_approved_paid_plan(paid_run):
    plan, report = paid_run
    if report["status"] == "recovered_existing":
        pytest.skip("Existing receipts recovered; no fresh inference acceptance claimed")
    assert report["status"] == "passed_execution"
    assert report["plan_digest"] == plan.digest
    assert report["inference_posts_attempted"] == len(plan.cases)
    assert len(report["cases"]) == len(plan.cases)
    assert all(case["status"] == "passed_execution" for case in report["cases"])
    assert all(case["receipts"] and case["model"]["actual_model"] for case in report["cases"])
    if plan.max_concurrency == 2:
        assert report["concurrency_evidence"] == "client_call_overlap"
