"""Acceptance reports distinguish execution, availability and billing evidence."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from test_compatibility_boundaries import result
from test_paid_executor import approval_for

from llm_gateway.client import GatewayHTTPError, GatewayTransportError
from llm_gateway.contracts import TokenUsage
from llm_gateway.providers.runtime import ProviderFailure
from llm_gateway.testing import runner
from llm_gateway.testing.preflight import PreflightError

pytestmark = pytest.mark.offline
pytest_plugins = ["test_live_runner"]


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"execution_status": "in_progress", "output_status": "pending"}, "in_progress"),
        ({"execution_status": "failed"}, "failed_execution"),
        ({"output_status": "invalid_document"}, "failed_output"),
        ({"receipts": []}, "failed_missing_receipt_evidence"),
        (
            {"model": result().model.model_copy(update={"actual_model": None})},
            "failed_missing_model_evidence",
        ),
    ],
)
def test_incomplete_acceptance_evidence_cannot_be_classified_as_pass(change, expected):
    value = result().model_copy(update=change)
    case = type("Case", (), {"case_id": "case", "request": value})()
    record = runner._case_record(case, value)
    assert record["status"] == expected
    assert "content" not in record


@pytest.mark.parametrize(
    "code",
    ["provider_auth", "provider_quota", "provider_unavailable", "provider_deadline_exceeded"],
)
def test_provider_unavailability_has_its_own_report_classification(live_double, code):
    plan, snapshot, client, engine, posts, _ = live_double
    engine.provider.failure = ProviderFailure(code, dispatched=False)
    report = runner.run_plan(plan, snapshot, client, enabled=True)
    assert report["status"] == "unavailable_precondition"
    assert report["cases"][0]["status"] == "unavailable_provider"
    assert report["cases"][0]["safe_error_code"] == code
    assert len(posts) == 1 and len(engine.provider.calls) == 1


def test_recovered_failed_or_pending_work_is_not_fresh_acceptance():
    value = result(execution_status="failed", output_status="unavailable")
    case = type("Case", (), {"case_id": "case", "request": value})()
    record = runner._case_record(case, value, recovered=True)
    assert record["status"] == "recovered_failed_or_pending_request"
    assert record["function_call_count"] is None and record["receipts"]


def test_free_report_rejects_actual_positive_observed_cost_and_keeps_ledger(live_double):
    plan, snapshot, client, engine, posts, _ = live_double
    engine.provider.cost = Decimal("0.01")  # Synthetic inference double, no provider.
    report = runner.run_plan(plan, snapshot, client, enabled=True)
    assert report["status"] == "failed_acceptance"
    assert report["cases"][0]["status"] == "failed_zero_cost_boundary"
    assert report["cases"][0]["receipts"][0]["measured_usd"] == "0.01"
    assert report["usage_after"]["overrun"] is True
    assert len(posts) == 1


@pytest.mark.parametrize("live_double", [True], indirect=True)
@pytest.mark.parametrize("missing_usage", [False, True])
def test_paid_report_keeps_estimates_and_unknown_billing_distinct(
    live_double, monkeypatch, missing_usage
):
    plan, snapshot, client, engine, posts, _ = live_double
    engine.provider.cost = None
    if missing_usage:
        original = engine.provider.invoke
        monkeypatch.setattr(
            engine.provider,
            "invoke",
            lambda *args, **kwargs: replace(
                original(*args, **kwargs),
                usage=TokenUsage(),
            ),
        )
    report = runner.run_plan(
        plan,
        snapshot,
        client,
        enabled=True,
        paid_enabled=True,
        approval=approval_for(plan, snapshot),
    )
    assert report["status"] == (
        "failed_billing_boundary" if missing_usage else "execution_passed_billing_unverified"
    )
    assert report["billing_followup_required"] is True
    receipt = report["cases"][0]["receipts"][0]
    assert receipt["measured_usd"] is None
    assert receipt["evidence_status"] == ("unavailable" if missing_usage else "estimated")
    assert Decimal(receipt["protected_debit_usd"]) > 0
    assert len(posts) == 1


def test_result_configuration_identity_failure_keeps_receipts(live_double, monkeypatch):
    plan, snapshot, client, engine, posts, _ = live_double
    original = client.infer

    def changed(*args, **kwargs):
        return original(*args, **kwargs).model_copy(update={"config_digest": "0" * 64})

    monkeypatch.setattr(client, "infer", changed)
    report = runner.run_plan(plan, snapshot, client, enabled=True)
    assert report["status"] == "failed_acceptance"
    assert report["cases"][0]["status"] == "failed_configuration_identity"
    assert report["cases"][0]["receipts"] and len(posts) == 1


@pytest.mark.parametrize("kind", ["aliases", "existing_config", "lookup_denied", "ready_failure"])
def test_gateway_inspection_failures_cannot_submit(live_double, monkeypatch, kind):
    plan, snapshot, client, engine, posts, _ = live_double
    if kind == "aliases":
        aliases = client.aliases().model_copy(update={"aliases": []})
        monkeypatch.setattr(client, "aliases", lambda: aliases)
    elif kind == "existing_config":
        old = result(config_digest="0" * 64)
        monkeypatch.setattr(client, "get_request", lambda identifier: old)
    else:

        def fail(*args, **kwargs):
            raise GatewayHTTPError(403) if kind == "lookup_denied" else GatewayTransportError()

        monkeypatch.setattr(client, "get_request" if kind == "lookup_denied" else "ready", fail)
    report = runner.run_plan(plan, snapshot, client, enabled=True)
    assert report["status"] == "unrun_precondition"
    assert report["inference_posts_attempted"] == 0 and not posts and not engine.provider.calls


def test_failed_final_usage_query_never_erases_observed_receipts(live_double, monkeypatch):
    plan, snapshot, client, engine, posts, _ = live_double
    original, calls = client.usage, []

    def usage(**kwargs):
        calls.append(True)
        if len(calls) > 1:
            raise GatewayTransportError()
        return original(**kwargs)

    monkeypatch.setattr(client, "usage", usage)
    report = runner.run_plan(plan, snapshot, client, enabled=True)
    assert report["status"] == "passed_execution"
    assert report["usage_after"] is None
    assert report["usage_after_status"] == "unavailable_preserve_case_receipts"
    assert report["cases"][0]["receipts"] and len(posts) == 1


def test_transport_loss_without_recovery_is_unknown_and_never_retried(live_double, monkeypatch):
    plan, snapshot, client, engine, posts, _ = live_double
    original_lookup, reads = client.get_request, []

    def lookup(identifier):
        reads.append(True)
        if len(reads) > 1:
            raise GatewayTransportError(identifier)
        return original_lookup(identifier)

    def lost(request, **kwargs):
        raise GatewayTransportError(request.request_id)

    monkeypatch.setattr(client, "get_request", lookup)
    monkeypatch.setattr(client, "infer", lost)
    report = runner.run_plan(plan, snapshot, client, enabled=True)
    assert report["status"] == "failed_gateway_operation"
    assert report["cases"][0]["receipt_recovery"] == "unavailable_query_later_with_same_id"
    assert report["inference_posts_attempted"] == 1
    assert not posts and not engine.provider.calls


def test_private_report_rejections_and_failed_replacement_preserve_previous_checkpoint(
    tmp_path, monkeypatch
):
    with pytest.raises(PreflightError, match="absolute_path"):
        runner.ReportFile(Path("relative.json"))
    path = tmp_path / "report.json"
    evidence = runner.ReportFile(path)
    evidence.write({"status": "initial"})
    runner.write_report(tmp_path / "separate.json", {"status": "bounded"})
    with pytest.raises(PreflightError, match="size_limit"):
        evidence.write({"oversized": "x" * (16 * 1024 * 1024)})

    def failure(*args):
        raise OSError("synthetic atomic replacement failure")

    monkeypatch.setattr(runner.os, "replace", failure)
    with pytest.raises(OSError):
        evidence.write({"status": "later"})
    assert path.read_text().strip().startswith('{\n  "status": "initial"')
    assert not list(tmp_path.glob(".live-report-*"))
