"""Unsupported free function probes never replace smoke, output or cost gates."""

import json
import runpy
from dataclasses import replace
from decimal import Decimal

import pytest
from test_config import private_file, write_config
from test_paid_executor import approval_for

from llm_gateway.config import load_project_config
from llm_gateway.contracts import ChatInput, FunctionChoice, FunctionTool, Message, TokenUsage
from llm_gateway.providers.runtime import ProviderEvidence, ProviderFailure
from llm_gateway.testing import run
from llm_gateway.testing.runner import run_plan

pytestmark = pytest.mark.offline
pytest_plugins = ["test_live_runner"]
UNSUPPORTED = "provider_http_404_parameters_unavailable"


@pytest.fixture
def function_probe(live_double, tmp_path, monkeypatch):
    plan, snapshot, client, engine, posts, _ = live_double
    config = snapshot.config.model_dump(mode="json")
    config["aliases"]["summary"]["capabilities"].append("function_calls")
    snapshot = load_project_config(write_config(tmp_path, config), check_credentials=False)
    engine.registry.activate(snapshot)
    data = ChatInput(
        messages=[Message(role="user", content="Call record_check with ok true.")],
        tools=[
            FunctionTool(
                name="record_check",
                parameters={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            )
        ],
        tool_choice="required",
    )
    text = plan.cases[0]
    function = text.model_copy(
        update={
            "case_id": "function",
            "request": text.request.model_copy(update={"request_id": "function", "input": data}),
        }
    )
    plan = plan.model_copy(update={"cases": [function, text], "max_calls": 2})
    original = engine.provider.invoke
    failure = [ProviderFailure(UNSUPPORTED, dispatched=True)]

    def invoke(prepared, provider, **kwargs):
        if prepared.body.get("tools") and failure[0] is not None:
            engine.provider.calls.append(prepared)
            raise failure[0]
        return original(prepared, provider, **kwargs)

    monkeypatch.setattr(engine.provider, "invoke", invoke)
    return plan, snapshot, client, engine, posts, failure


def assert_free(report, plan, predicate, **kwargs):
    return runpy.run_path("tests/live/test_free.py")["assert_cases"](
        (plan, report), predicate, **kwargs
    )


@pytest.mark.parametrize("code", [UNSUPPORTED, "provider_http_404_tools_unavailable"])
@pytest.mark.parametrize("named", [False, True])
def test_known_free_function_rejection_keeps_baseline_and_receipts(function_probe, code, named):
    plan, snapshot, client, engine, posts, failure = function_probe
    if named:
        case = plan.cases[0]
        data = case.request.input.model_copy(
            update={"tool_choice": FunctionChoice(name="record_check")}
        )
        request = case.request.model_copy(update={"input": data})
        plan = plan.model_copy(
            update={"cases": [case.model_copy(update={"request": request}), plan.cases[1]]}
        )
    failure[0] = ProviderFailure(code, dispatched=True)
    report = run_plan(plan, snapshot, client, enabled=True)
    assert report["status"] == "passed_with_unsupported_capabilities"
    probe, baseline = report["cases"]
    assert probe["status"] == "unsupported_capability"
    assert probe["execution_status"] == "failed" and probe["safe_error_code"] == code
    assert probe["optional_capability"] == "function_calls"
    assert probe["receipts"][0]["protected_debit_usd"] == "0"
    assert probe["receipts"][0]["measured_usd"] is None
    assert baseline["status"] == "passed_execution"
    assert len(posts) == len(engine.provider.calls) == report["usage_after"]["attempts"] == 2
    assert_free(report, plan, lambda data: not data.tools, optional=False)
    with pytest.raises(pytest.skip.Exception, match="capability unsupported"):
        assert_free(report, plan, lambda data: bool(data.tools), capability_probe=True)
    assert "private-output-canary" not in json.dumps(report)


def test_probe_only_is_not_a_smoke_pass_and_recovery_never_redispatches(function_probe):
    plan, snapshot, client, engine, posts, _ = function_probe
    plan = plan.model_copy(update={"cases": plan.cases[:1], "max_calls": 1})
    first = run_plan(plan, snapshot, client, enabled=True)
    assert first["status"] == "unsupported_capability"
    again = run_plan(plan, snapshot, client, enabled=True)
    assert again["cases"][0]["status"] == "recovered_failed_or_pending_request"
    assert again["status"] == "unrun_precondition"
    assert len(posts) == len(engine.provider.calls) == 1


@pytest.mark.parametrize(
    "code,expected",
    [
        ("provider_auth", "unavailable_precondition"),
        ("provider_quota", "unavailable_precondition"),
        ("provider_http_404", "failed_acceptance"),
        ("provider_http_404_data_policy", "failed_acceptance"),
        ("provider_http_500", "failed_acceptance"),
        ("provider_transport_failed", "failed_acceptance"),
    ],
)
def test_other_function_failures_are_not_unsupported(function_probe, code, expected):
    plan, snapshot, client, engine, posts, failure = function_probe
    failure[0] = ProviderFailure(code, dispatched=True)
    report = run_plan(plan, snapshot, client, enabled=True)
    assert report["status"] == expected
    assert report["cases"][0]["status"] != "unsupported_capability"
    assert report["cases"][1]["status"] == "unrun"
    assert len(posts) == len(engine.provider.calls) == 1


@pytest.mark.parametrize("hazard", ["cost", "missing_receipt", "identity", "not_dispatched"])
def test_capability_rejection_cannot_hide_evidence_boundary_failure(
    function_probe, monkeypatch, hazard
):
    plan, snapshot, client, engine, _, failure = function_probe
    if hazard == "cost":
        failure[0] = ProviderFailure(
            UNSUPPORTED,
            dispatched=True,
            evidence=ProviderEvidence({}, TokenUsage(), Decimal("0.01"), None, None, None),
        )
    elif hazard == "not_dispatched":
        failure[0] = ProviderFailure(UNSUPPORTED, dispatched=False)
    else:
        original = client.infer

        def infer(*args, **kwargs):
            value = original(*args, **kwargs)
            return value.model_copy(
                update={"receipts": []} if hazard == "missing_receipt" else {"project_id": "other"}
            )

        monkeypatch.setattr(client, "infer", infer)
    report = run_plan(plan, snapshot, client, enabled=True)
    assert report["status"] == "failed_acceptance"
    assert report["cases"][0]["status"] != "unsupported_capability"
    assert len(engine.provider.calls) == 1


@pytest.mark.parametrize("outcome", ["no_function", "valid_function"])
def test_successful_provider_must_actually_return_a_function(function_probe, monkeypatch, outcome):
    plan, snapshot, client, engine, _, failure = function_probe
    failure[0] = None
    if outcome == "valid_function":
        original = engine.provider.invoke

        def invoke(prepared, provider, **kwargs):
            value = original(prepared, provider, **kwargs)
            if prepared.body.get("tools"):
                return replace(
                    value,
                    payload={
                        "choices": [
                            {
                                "finish_reason": "tool_calls",
                                "message": {
                                    "tool_calls": [
                                        {
                                            "id": "call-one",
                                            "type": "function",
                                            "function": {
                                                "name": "record_check",
                                                "arguments": '{"ok":true}',
                                            },
                                        }
                                    ]
                                },
                            }
                        ]
                    },
                )
            return value

        monkeypatch.setattr(engine.provider, "invoke", invoke)
    report = run_plan(plan, snapshot, client, enabled=True)
    assert report["status"] == (
        "failed_acceptance" if outcome == "no_function" else "passed_execution"
    )
    assert report["cases"][0]["status"] == (
        "failed_execution" if outcome == "no_function" else "passed_execution"
    )
    if outcome == "valid_function":
        records = assert_free(report, plan, lambda data: bool(data.tools), capability_probe=True)
        assert records[0]["function_call_count"] == 1


@pytest.mark.parametrize("live_double", [True], indirect=True)
def test_paid_function_rejection_remains_failed(function_probe):
    plan, snapshot, client, engine, _, _ = function_probe
    plan = plan.model_copy(update={"cases": plan.cases[:1], "max_calls": 1})
    report = run_plan(
        plan,
        snapshot,
        client,
        enabled=True,
        paid_enabled=True,
        approval=approval_for(plan, snapshot),
    )
    assert report["status"] == "failed_acceptance"
    assert report["cases"][0]["status"] == "failed_execution"
    assert "optional_capability" not in report["cases"][0]
    assert len(engine.provider.calls) == 1


def test_optional_probe_does_not_hide_later_baseline_failure(function_probe):
    plan, snapshot, client, engine, _, _ = function_probe
    engine.provider.cost = Decimal("0.01")
    report = run_plan(plan, snapshot, client, enabled=True)
    assert report["status"] == "failed_acceptance"
    assert report["cases"][0]["status"] == "unsupported_capability"
    assert report["cases"][1]["status"] == "failed_zero_cost_boundary"
    with pytest.raises(AssertionError):
        assert_free(report, plan, lambda data: bool(data.tools), capability_probe=True)


def test_cli_reports_partial_acceptance_without_claiming_function_support(
    function_probe, tmp_path, monkeypatch
):
    plan, _, client, engine, _, _ = function_probe
    plan_path = private_file(tmp_path, "plan.json", plan.model_dump_json())
    monkeypatch.setattr(run, "GatewayClient", lambda *args, **kwargs: client)
    output = tmp_path / "cli-report.json"
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
                str(output),
                "--enable-free",
            ]
        )
        == 0
    )
    report = json.loads(output.read_text())
    assert report["status"] == "passed_with_unsupported_capabilities"
    assert report["cases"][0]["status"] == "unsupported_capability"
    assert len(engine.provider.calls) == 2


@pytest.mark.parametrize("first_status", ["unsupported_capability", "recovered_existing", "unrun"])
def test_other_selected_function_failure_is_not_hidden_by_an_earlier_skip(
    function_probe, monkeypatch, first_status
):
    plan, snapshot, client, engine, _, failure = function_probe
    first = plan.cases[0]
    second = first.model_copy(
        update={
            "case_id": "other-function",
            "request": first.request.model_copy(update={"request_id": "other-function"}),
        }
    )
    plan = plan.model_copy(update={"cases": [first, second]})
    original = engine.provider.invoke

    def invoke(*args, **kwargs):
        if engine.provider.calls:
            failure[0] = ProviderFailure("provider_http_500", dispatched=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine.provider, "invoke", invoke)
    report = run_plan(plan, snapshot, client, enabled=True)
    assert [c["status"] for c in report["cases"]] == ["unsupported_capability", "failed_execution"]
    report["cases"][0]["status"] = first_status
    with pytest.raises(AssertionError, match="provider_http_500"):
        assert_free(report, plan, lambda data: bool(data.tools), capability_probe=True)


def test_nonforced_function_request_is_not_an_optional_strict_probe(function_probe):
    plan, snapshot, client, engine, _, _ = function_probe
    case = plan.cases[0]
    data = case.request.input.model_copy(update={"tool_choice": "auto"})
    request = case.request.model_copy(update={"input": data})
    plan = plan.model_copy(update={"cases": [case.model_copy(update={"request": request})]})
    report = run_plan(plan, snapshot, client, enabled=True)
    assert report["status"] == "failed_acceptance"
    assert "optional_capability" not in report["cases"][0]
    assert len(engine.provider.calls) == 1
