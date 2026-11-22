import json
import runpy
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from test_config import private_file, write_config
from test_engine import ProviderDouble
from test_live_preflight import setup

from llm_gateway.client import GatewayClient
from llm_gateway.config import load_project_config
from llm_gateway.contracts import ChatInput, Message
from llm_gateway.providers.runtime import ProviderFailure
from llm_gateway.service.api import create_app
from llm_gateway.service.engine import Engine
from llm_gateway.storage import StateStore
from llm_gateway.testing.runner import ReportFile, run_plan

pytestmark = pytest.mark.offline


@pytest.fixture
def live_double(tmp_path, monkeypatch, request):
    plan, original = setup(tmp_path, paid=getattr(request, "param", False))
    req = plan.cases[0].request.model_copy(
        update={"input": ChatInput(messages=[Message(role="user", content="private-input-canary")])}
    )
    plan = plan.model_copy(update={"cases": [plan.cases[0].model_copy(update={"request": req})]})
    data = original.config.model_dump(mode="json")
    token = private_file(tmp_path, "test.token", "t" * 48)
    data["access"]["principals"]["test"]["token_file"] = str(token)
    snapshot = load_project_config(write_config(tmp_path, data), check_credentials=False)
    monkeypatch.setenv("EXAMPLE_ROUTER_KEY", "k" * 48)
    provider = ProviderDouble(content="private-output-canary", cost=Decimal("0"))
    engine = Engine(StateStore(tmp_path / "state"), provider=provider)
    engine.registry.activate(snapshot)
    app = create_app(engine)
    with TestClient(app) as server:
        posts = []
        lose_reply = [False]

        def send(request):
            response = server.request(
                request.method,
                request.url.raw_path.decode(),
                content=request.content,
                headers=request.headers,
            )
            if request.method == "POST" and request.url.path == "/v1/inferences":
                posts.append(request)
                if lose_reply[0]:
                    lose_reply[0] = False
                    raise httpx.ReadError("synthetic reply loss")
            return httpx.Response(
                response.status_code, content=response.content, headers=response.headers
            )

        with GatewayClient(
            "http://gateway.test", token_file=token, transport=httpx.MockTransport(send)
        ) as client:
            yield plan, snapshot, client, engine, posts, lose_reply


def test_authenticated_free_double_records_no_content_and_rerun_never_redispatches(
    live_double, tmp_path
):
    plan, snapshot, client, engine, posts, _ = live_double
    evidence = ReportFile(tmp_path / "report.json")
    first = run_plan(plan, snapshot, client, enabled=True, report_sink=evidence.write)
    assert first["status"] == "passed_execution"
    assert first["usage_after"]["attempts"] == 1
    assert first["usage_after"]["protected_debit_usd"] == "0"
    assert "private-output-canary" not in json.dumps(first)
    assert "private-input-canary" not in json.dumps(first)
    assert json.loads((tmp_path / "report.json").read_text()) == first
    assert (tmp_path / "report.json").stat().st_mode & 0o077 == 0
    rerun = run_plan(plan, snapshot, client, enabled=True)
    assert rerun["status"] == "recovered_existing"
    assert len(posts) == 2 and len(engine.provider.calls) == 1


def test_transport_loss_queries_receipt_without_retrying_post(live_double):
    plan, snapshot, client, engine, posts, lost = live_double
    lost[0] = True
    report = run_plan(plan, snapshot, client, enabled=True)
    assert report["status"] == "failed_gateway_operation"
    assert report["cases"][0]["receipts"][0]["measured_usd"] == "0"
    assert report["usage_after"]["attempts"] == 1
    assert len(posts) == 1 and len(engine.provider.calls) == 1


def test_safe_provider_diagnostic_survives_ledger_report_and_pytest_failure(live_double):
    plan, snapshot, client, engine, posts, _ = live_double
    code = "provider_http_404_parameters_unavailable"
    engine.provider.failure = ProviderFailure(code, dispatched=True)
    report = run_plan(plan, snapshot, client, enabled=True)
    case = plan.cases[0]
    assert report["cases"][0]["safe_error_code"] == code
    attempt = engine.ledger.attempts(plan.project_id, plan.principal_id, case.request.request_id)
    assert len(attempt) == 1 and attempt[0]["error_code"] == code
    assert report["usage_after"]["protected_debit_usd"] == "0"
    assert report["usage_after"]["unknown_attempts"] == 1
    assert len(posts) == 1 and len(engine.provider.calls) == 1
    assert_cases = runpy.run_path("tests/live/test_free.py")["assert_cases"]
    with pytest.raises(AssertionError, match=code):
        assert_cases((plan, report), lambda data: True)
    assert "private-input-canary" not in json.dumps(report)


def test_wrong_server_digest_and_disabled_group_never_submit(live_double, monkeypatch):
    plan, snapshot, client, engine, posts, _ = live_double
    assert (
        run_plan(plan, snapshot, client)["precondition"]
        == "live_group_explicit_enablement_required"
    )
    ready = client.ready()
    for change in [
        {"config_digest": "0" * 64},
        {"project_id": "other"},
        {"principal_id": "other"},
        {"principal_id": None},
    ]:
        monkeypatch.setattr(client, "ready", lambda change=change: ready.model_copy(update=change))
        report = run_plan(plan, snapshot, client, enabled=True)
        assert report["precondition"] == "live_active_configuration_mismatch"
    assert not posts and not engine.provider.calls


def test_older_server_without_atomic_precondition_never_submits(live_double, monkeypatch):
    plan, snapshot, client, engine, posts, _ = live_double
    ready = client.ready().model_copy(update={"supports_config_precondition": False})
    monkeypatch.setattr(client, "ready", lambda: ready)
    report = run_plan(plan, snapshot, client, enabled=True)
    assert report["precondition"] == "live_server_configuration_precondition_required"
    assert not posts and not engine.provider.calls


def test_reload_after_runner_inspection_never_dispatches(live_double, monkeypatch, tmp_path):
    plan, snapshot, client, engine, posts, _ = live_double
    original_infer = client.infer

    def raced(request, *, expected_config_digest):
        data = snapshot.config.model_dump(mode="json")
        data["aliases"][plan.cases[0].request.alias]["limits"]["max_output_tokens"] -= 1
        new = load_project_config(write_config(tmp_path, data), check_credentials=False)
        engine.registry.activate(new)
        return original_infer(request, expected_config_digest=expected_config_digest)

    monkeypatch.setattr(client, "infer", raced)
    report = run_plan(plan, snapshot, client, enabled=True)
    assert report["status"] == "failed_gateway_operation"
    assert report["cases"][0]["http_status"] == 409
    assert len(posts) == 1 and not engine.provider.calls
    assert engine.ledger.report(snapshot.config.project_id)["attempts"] == 0


def test_paid_execution_requires_separate_paid_enablement(tmp_path):
    plan, snapshot = setup(tmp_path, paid=True)

    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError("paid gate must precede every gateway method")

    report = run_plan(plan, snapshot, Forbidden(), enabled=True)
    assert report["precondition"] == "paid_execution_waits_for_final_owner_approval"
    assert report["inference_posts_attempted"] == 0 and report["usage_before"] is None


def test_existing_evidence_is_never_overwritten_and_checkpoint_failure_stops_dispatch(
    live_double, tmp_path
):
    plan, snapshot, client, engine, posts, _ = live_double
    evidence = ReportFile(tmp_path / "report.json")
    with pytest.raises(FileExistsError):
        ReportFile(tmp_path / "report.json")
    checkpoints = []

    def fail_before_post(report):
        checkpoints.append(report["inference_posts_attempted"])
        if report["inference_posts_attempted"]:
            raise OSError("synthetic disk failure")
        evidence.write(report)

    with pytest.raises(OSError):
        run_plan(plan, snapshot, client, enabled=True, report_sink=fail_before_post)
    assert not posts and not engine.provider.calls
    assert json.loads((tmp_path / "report.json").read_text())["inference_posts_attempted"] == 0


def test_zero_call_allowance_is_enforced_from_the_persistent_service_ledger(live_double):
    plan, snapshot, client, engine, posts, _ = live_double
    first = run_plan(plan, snapshot, client, enabled=True)
    assert first["status"] == "passed_execution"
    other = plan.model_copy(
        update={
            "cases": [
                plan.cases[0].model_copy(
                    update={
                        "request": plan.cases[0].request.model_copy(update={"request_id": "new-id"})
                    }
                )
            ]
        }
    )
    report = run_plan(other, snapshot, client, enabled=True)
    assert report["precondition"] == "live_persistent_ledger_allowance_exhausted"
    assert report["cases"][0]["status"] == "unrun"
    assert len(posts) == 1 and len(engine.provider.calls) == 1


def test_interrupt_never_labels_an_attempted_submission_unrun(live_double, monkeypatch, tmp_path):
    plan, snapshot, client, engine, posts, _ = live_double

    def interrupted(request, *, expected_config_digest=None):
        assert expected_config_digest == snapshot.digest
        raise KeyboardInterrupt()

    monkeypatch.setattr(client, "infer", interrupted)
    report = run_plan(
        plan, snapshot, client, enabled=True, report_sink=ReportFile(tmp_path / "report.json").write
    )
    assert report["status"] == "interrupted_preserve_ledger_and_ids"
    assert report["cases"][0]["status"] == "submitted_outcome_unknown"
    assert report["inference_posts_attempted"] == 1
    assert not posts and not engine.provider.calls


def test_report_symlink_substitution_never_overwrites_another_file(tmp_path):
    from llm_gateway.testing.preflight import PreflightError

    path, victim = tmp_path / "report.json", tmp_path / "victim"
    evidence = ReportFile(path)
    evidence.write({"status": "initial"})
    victim.write_text("preserve")
    path.unlink()
    path.symlink_to(victim)
    with pytest.raises(PreflightError, match="identity_changed"):
        evidence.write({"status": "later"})
    assert victim.read_text() == "preserve"


def test_cli_verifies_scope_with_explicit_client_token_path_and_private_report(
    live_double, tmp_path, monkeypatch
):
    from llm_gateway.testing import run

    plan, snapshot, client, engine, _, _ = live_double
    plan_path = private_file(tmp_path, "plan.json", plan.model_dump_json(indent=2))
    client_token = private_file(tmp_path, "client-copy.token", "t" * 48)

    def client_factory(origin, **kwargs):
        assert origin == "http://gateway.test"
        assert kwargs["token_file"] == client_token
        return client

    monkeypatch.setattr(run, "GatewayClient", client_factory)
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
                "--gateway-token-file",
                str(client_token),
                "--enable-free",
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["status"] == "passed_execution"
    assert len(engine.provider.calls) == 1
