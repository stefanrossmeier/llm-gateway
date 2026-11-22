"""Synthetic paid approvals and provider doubles; never network inference."""

import importlib
import json
import threading
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_config import private_file, write_config
from test_paid_executor import approval_for
from test_paid_proposal import project
from test_parallel_paid_plans import pair

from llm_gateway.client import GatewayClient, GatewayTransportError
from llm_gateway.config import load_project_config
from llm_gateway.contracts import TokenUsage
from llm_gateway.providers.runtime import ProviderEvidence, ProviderFailure
from llm_gateway.service.api import create_app
from llm_gateway.service.engine import Engine
from llm_gateway.storage import StateStore
from llm_gateway.testing.runner import ReportFile, run_plan

pytestmark = pytest.mark.offline
runner = importlib.import_module("llm_gateway.testing.runner")


class PairProvider:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(2)
        self.fail = None

    def invoke(self, prepared, provider, **kwargs):
        with self.lock:
            self.calls.append(provider.kind)
        if self.barrier:
            self.barrier.wait(10)
        payload = (
            {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "READY"}]}
                ],
            }
            if provider.kind == "openai"
            else {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]}
        )
        evidence = ProviderEvidence(
            payload,
            TokenUsage(input_tokens=3, output_tokens=2),
            Decimal("0.000001"),
            f"observed-{provider.kind}",
            None,
            f"gen-{provider.kind}",
        )
        if self.fail == provider.kind:
            raise ProviderFailure("provider_failed", dispatched=True, evidence=evidence)
        return evidence


@pytest.fixture
def concurrent_double(tmp_path, monkeypatch):
    original = project(tmp_path)
    data = original.config.model_dump(mode="json")
    token = private_file(tmp_path, "pair.token", "t" * 48)
    data["access"]["principals"]["test"]["token_file"] = str(token)
    for alias in data["aliases"].values():
        alias["limits"].update(attempt_timeout_seconds=5, request_timeout_seconds=30)
    snapshot = load_project_config(write_config(tmp_path, data), check_credentials=False)
    for provider in snapshot.config.providers.values():
        monkeypatch.setenv(provider.credential.env_var, "synthetic-key-never-sent-to-provider")
    plan = pair(snapshot)["plan"]
    provider = PairProvider()
    engine = Engine(StateStore(tmp_path / "state"), provider=provider)
    engine.registry.activate(snapshot)
    posts = []
    with TestClient(create_app(engine)) as server:

        def send(request):
            if request.method == "POST":
                posts.append(request)
            response = server.request(
                request.method,
                request.url.raw_path.decode(),
                content=request.content,
                headers=request.headers,
            )
            return httpx.Response(
                response.status_code, content=response.content, headers=response.headers
            )

        with GatewayClient(
            "http://gateway.test", token_file=token, transport=httpx.MockTransport(send)
        ) as client:
            yield plan, snapshot, client, engine, posts


def execute(fixture, **kwargs):
    plan, snapshot, client, _, _ = fixture
    return run_plan(
        plan,
        snapshot,
        client,
        enabled=True,
        paid_enabled=True,
        approval=approval_for(plan, snapshot),
        **kwargs,
    )


class SerialExecutor:
    """Deterministically prove that two completed calls do not imply overlap."""

    def __init__(self, **kwargs):
        self.futures = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, function, *args):
        future = Future()
        self.futures.append(future)
        try:
            future.set_result(function(*args))
        except BaseException as error:
            future.set_exception(error)
        return future


def test_both_routes_overlap_with_atomic_truthful_private_checkpoints(concurrent_double, tmp_path):
    plan, snapshot, _, engine, posts = concurrent_double
    checkpoints = []
    evidence = ReportFile(tmp_path / "pair-report.json")

    def checkpoint(report):
        checkpoints.append(json.loads(json.dumps(report)))
        evidence.write(report)

    report = execute(concurrent_double, report_sink=checkpoint)
    assert report["status"] == "passed_execution"
    assert report["concurrency_evidence"] == "client_call_overlap"
    assert len(posts) == 2 and sorted(engine.provider.calls) == ["openai", "openrouter"]
    assert all(
        post.headers["X-Gateway-Expected-Config-Digest"] == snapshot.digest for post in posts
    )
    assert report["usage_after"]["measured_usd"] == "0.000002"
    assert all(len(case["receipts"]) == 1 for case in report["cases"])
    assert all(case["client_inference_finished_at"] for case in report["cases"])
    assert any(item["status"] == "running" for item in checkpoints)
    for item in checkpoints:
        if any(case["status"] in {"unrun", "submitted_outcome_unknown"} for case in item["cases"]):
            assert item["status"] != "passed_execution"
    assert json.loads(evidence.path.read_text()) == report
    assert all(
        case.request.input.model_dump_json() not in json.dumps(report) for case in plan.cases
    )


def test_one_provider_failure_preserves_both_known_charges(concurrent_double):
    _, _, _, engine, posts = concurrent_double
    engine.provider.fail = "openai"
    report = execute(concurrent_double)
    assert report["status"] == "failed_acceptance"
    assert sorted(case["status"] for case in report["cases"]) == [
        "failed_execution",
        "passed_execution",
    ]
    assert report["usage_after"]["attempts"] == 2
    assert report["usage_after"]["measured_usd"] == "0.000002"
    assert all(case["receipts"][0]["measured_usd"] == "0.000001" for case in report["cases"])
    assert len(posts) == len(engine.provider.calls) == 2


def test_reply_loss_has_deterministic_priority_and_recovers_both_charges(
    concurrent_double, monkeypatch
):
    plan, _, client, engine, posts = concurrent_double
    original = client.infer
    engine.provider.fail = "openrouter"

    def lost_reply(request, **kwargs):
        result = original(request, **kwargs)
        if request == plan.cases[0].request:
            raise GatewayTransportError("synthetic reply loss")
        return result

    monkeypatch.setattr(client, "infer", lost_reply)
    report = execute(concurrent_double)
    assert report["status"] == "failed_gateway_operation"
    assert report["cases"][0]["recorded_execution_status"] == "completed"
    assert report["cases"][1]["status"] == "failed_execution"
    assert all(case["receipts"][0]["measured_usd"] == "0.000001" for case in report["cases"])
    assert len(posts) == len(engine.provider.calls) == 2


def test_complete_rerun_recovers_original_ids_without_fresh_provider_calls(concurrent_double):
    first = execute(concurrent_double)
    concurrent_double[3].provider.barrier = None
    again = execute(concurrent_double)
    assert first["status"] == "passed_execution"
    assert again["status"] == "recovered_existing"
    assert again["concurrency_evidence"] == "recovered_requests_not_fresh"
    assert len(concurrent_double[3].provider.calls) == 2
    assert [case["receipts"] for case in again["cases"]] == [
        case["receipts"] for case in first["cases"]
    ]


def test_partial_recovery_cannot_claim_fresh_concurrent_acceptance(concurrent_double):
    plan, snapshot, client, engine, posts = concurrent_double
    engine.provider.barrier = None
    original = client.infer(plan.cases[0].request, expected_config_digest=snapshot.digest)
    report = execute(concurrent_double)
    assert report["status"] == "partial_recovery_no_fresh_concurrency"
    assert report["concurrency_evidence"] == "recovered_requests_not_fresh"
    assert report["cases"][0]["receipts"] == [r.model_dump(mode="json") for r in original.receipts]
    assert len(posts) == 3 and len(engine.provider.calls) == 2


def test_two_successes_without_overlap_are_not_concurrent_acceptance(
    concurrent_double, monkeypatch
):
    monkeypatch.setattr(runner, "ThreadPoolExecutor", SerialExecutor)
    concurrent_double[3].provider.barrier = None
    report = execute(concurrent_double)
    assert report["status"] == "failed_concurrency_evidence"
    assert report["concurrency_evidence"] == "no_fresh_client_overlap"
    assert report["usage_after"]["measured_usd"] == "0.000002"


def test_stop_during_checkpoint_prevents_both_unsubmitted_calls(concurrent_double, monkeypatch):
    stop = threading.Event()
    monkeypatch.setattr(
        runner, "threading", SimpleNamespace(RLock=threading.RLock, Event=lambda: stop)
    )
    monkeypatch.setattr(runner, "ThreadPoolExecutor", SerialExecutor)

    def checkpoint(report):
        if report["inference_posts_attempted"]:
            stop.set()

    report = execute(concurrent_double, report_sink=checkpoint)
    assert report["status"] == "unrun_precondition"
    assert report["inference_posts_attempted"] == 0
    assert all(case["status"] == "unrun_execution_stopped" for case in report["cases"])
    assert not concurrent_double[4] and not concurrent_double[3].provider.calls


def test_checkpoint_failure_stops_waiting_peer_without_any_post(concurrent_double, monkeypatch):
    monkeypatch.setattr(runner, "ThreadPoolExecutor", SerialExecutor)

    def checkpoint(report):
        if report["inference_posts_attempted"]:
            raise OSError("synthetic checkpoint failure")

    with pytest.raises(OSError, match="checkpoint failure"):
        execute(concurrent_double, report_sink=checkpoint)
    assert not concurrent_double[4] and not concurrent_double[3].provider.calls


def test_pool_submission_failure_is_truthfully_unrun(concurrent_double, monkeypatch):
    class BrokenPool(SerialExecutor):
        def submit(self, *args):
            raise RuntimeError("synthetic unavailable executor")

    monkeypatch.setattr(runner, "ThreadPoolExecutor", BrokenPool)
    report = execute(concurrent_double)
    assert report["status"] == "unrun_precondition"
    assert report["precondition"] == "live_parallel_executor_unavailable"
    assert not concurrent_double[4] and not concurrent_double[3].provider.calls


def test_keyboard_interrupt_cancels_pending_calls_before_pool_exit(concurrent_double, monkeypatch):
    class InterruptFuture(Future):
        def result(self):
            raise KeyboardInterrupt()

    class InterruptedPool(SerialExecutor):
        def submit(self, *args):
            future = InterruptFuture()
            self.futures.append(future)
            return future

        def __exit__(self, *args):
            assert all(future.cancelled() for future in self.futures)
            return False

    monkeypatch.setattr(runner, "ThreadPoolExecutor", InterruptedPool)
    report = execute(concurrent_double)
    assert report["status"] == "interrupted_preserve_ledger_and_ids"
    assert report["inference_posts_attempted"] == 0
    assert not concurrent_double[4]


def test_expiry_at_checkpoint_never_submits_either_route(concurrent_double, monkeypatch):
    plan, snapshot, _, engine, posts = concurrent_double
    now = datetime.now(UTC)
    approval = approval_for(plan, snapshot, now=now)
    current = [now]

    class Clock:
        @staticmethod
        def now(tz):
            return current[0]

    monkeypatch.setattr(importlib.import_module("llm_gateway.testing.preflight"), "datetime", Clock)
    monkeypatch.setattr(runner, "ThreadPoolExecutor", SerialExecutor)

    def checkpoint(report):
        if report["inference_posts_attempted"]:
            current[0] = approval.expires_at + timedelta(seconds=1)

    report = run_plan(
        plan,
        snapshot,
        concurrent_double[2],
        enabled=True,
        paid_enabled=True,
        approval=approval,
        report_sink=checkpoint,
    )
    assert report["status"] == "unrun_precondition"
    assert report["inference_posts_attempted"] == 0
    assert not posts and not engine.provider.calls


@pytest.mark.parametrize("field", ["request_id", "project_id", "principal_id", "config_digest"])
def test_mismatched_receipt_identity_retains_charge_and_fails(
    concurrent_double, monkeypatch, field
):
    client = concurrent_double[2]
    original = client.infer

    def corrupted(request, **kwargs):
        result = original(request, **kwargs)
        if request == concurrent_double[0].cases[0].request:
            receipt = result.receipts[0].model_copy(
                update={field: "0" * 64 if field == "config_digest" else "other"}
            )
            return result.model_copy(update={"receipts": [receipt]})
        return result

    monkeypatch.setattr(client, "infer", corrupted)
    report = execute(concurrent_double)
    assert report["status"] == "failed_acceptance"
    assert report["cases"][0]["status"] == "failed_configuration_identity"
    assert report["usage_after"]["measured_usd"] == "0.000002"
