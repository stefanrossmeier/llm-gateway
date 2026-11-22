"""Actual-inference tests, selected only with explicit free-plan authorization."""

import os
from pathlib import Path

import pytest

from llm_gateway.client import GatewayClient, GatewayError
from llm_gateway.config import ConfigError, load_project_config
from llm_gateway.contracts import FunctionChoice
from llm_gateway.testing.preflight import PreflightError, load_plan
from llm_gateway.testing.runner import ReportFile, run_plan

pytestmark = pytest.mark.live_free


@pytest.fixture(scope="session")
def free_run(request):
    # A session fixture precedes function autouse guards; gate here as well.
    if (
        not request.config.getoption("--run-live-free")
        or os.environ.get("LLM_GATEWAY_ENABLE_FREE_TESTS") != "1"
    ):
        pytest.skip("Actual free inference requires marker selection, flag and explicit enablement")
    names = ["--live-plan", "--live-project-config", "--live-gateway-origin", "--live-report"]
    values = {name: request.config.getoption(name) for name in names}
    if not all(values.values()):
        pytest.skip(
            "Unavailable precondition: explicit project plan/config/gateway/private report required"
        )
    failure = None
    try:
        plan = load_plan(Path(values["--live-plan"]))
        if plan.group != "free":
            raise PreflightError("free_pytest_group_cannot_select_paid_plan")
        snapshot = load_project_config(
            Path(values["--live-project-config"]), check_credentials=False
        )
        principal = snapshot.config.access.principals[plan.principal_id]
        output = ReportFile(Path(values["--live-report"]))
        token = request.config.getoption("--live-gateway-token-file") or principal.token_file
        timeout = min(
            3610,
            max(a.limits.request_timeout_seconds for a in snapshot.config.aliases.values()) + 10,
        )
        with GatewayClient(
            values["--live-gateway-origin"], token_file=token, timeout_seconds=timeout
        ) as client:
            report = run_plan(plan, snapshot, client, enabled=True, report_sink=output.write)
        return plan, report
    except (ConfigError, PreflightError, GatewayError, OSError, ValueError, KeyError):
        failure = "Explicit free setup failed safely; inspect private configuration/report"
    # Outside the handler so pytest cannot print private validation exception context.
    pytest.fail(failure, pytrace=False)


def assert_cases(free_run, predicate, *, optional=True, capability_probe=False):
    plan, report = free_run
    selected = [c for c in plan.cases if predicate(c.request.input)]
    if not selected:
        if optional:
            pytest.skip("Project plan does not select this optional free capability")
        pytest.fail("The free smoke plan requires a plain-text baseline case", pytrace=False)
    if report["status"] in {"unrun_precondition", "unavailable_precondition"}:
        pytest.skip(
            "Unavailable free precondition; safe status and receipts are in the private report"
        )
    records = {case["case_id"]: case for case in report["cases"]}
    unsupported = []
    not_fresh = []
    for case in selected:
        record = records[case.case_id]
        if record["status"] == "unrun":
            not_fresh.append("Case was not run after an earlier failure; no success claimed")
            continue
        if record["status"] == "recovered_existing":
            not_fresh.append("Existing receipts recovered; no new inference claimed or purchased")
            continue
        if capability_probe and record["status"] == "unsupported_capability":
            unsupported.append(record["safe_error_code"])
            continue
        assert record["status"] == "passed_execution", (
            f"Free case {case.case_id}: status={record['status']}, "
            f"safe_error_code={record.get('safe_error_code')}"
        )
        assert record["model"]["actual_model"]
        assert record["receipts"]
        assert all(r["protected_debit_usd"] == "0" for r in record["receipts"])
    if unsupported:
        assert report["status"] in {
            "passed_with_unsupported_capabilities",
            "unsupported_capability",
        }
        pytest.skip("Optional free function capability unsupported: " + ", ".join(unsupported))
    if not_fresh:
        pytest.skip("; ".join(not_fresh))
    return [records[case.case_id] for case in selected]


def test_free_text_actual_inference(free_run):
    assert_cases(
        free_run,
        lambda data: data.kind == "chat" and data.response_format == "text" and not data.tools,
        optional=False,
    )


def test_free_structured_output_when_selected_and_supported(free_run):
    assert_cases(
        free_run,
        lambda data: data.kind == "chat" and data.response_format != "text" and not data.tools,
    )


def test_free_function_calls_when_selected_and_supported(free_run):
    records = assert_cases(
        free_run,
        lambda data: (
            data.kind == "chat"
            and data.tools
            and (data.tool_choice == "required" or isinstance(data.tool_choice, FunctionChoice))
        ),
        capability_probe=True,
    )
    assert all(record["function_call_count"] > 0 for record in records)
