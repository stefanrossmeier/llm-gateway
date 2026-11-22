"""MODEL-04–13, COST-06–17, SEC-09–11: same pipeline for fake and live doubles."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from test_config import synthetic_config, write_config
from test_ledger import request

from llm_gateway.config import load_project_config
from llm_gateway.config.models import PrincipalConfig
from llm_gateway.contracts import TokenUsage
from llm_gateway.providers.runtime import ProviderEvidence, ProviderFailure
from llm_gateway.service.engine import Authority, Engine, EngineError
from llm_gateway.storage import StateStore
from llm_gateway.storage.outbox import export_trace

pytestmark = pytest.mark.offline


class ProviderDouble:
    def __init__(self, *, failure=None, content="ok", cost=Decimal("0.0001")):
        self.calls, self.failure, self.content, self.cost = [], failure, content, cost

    def invoke(self, prepared, provider, **kwargs):
        self.calls.append(prepared)
        if self.failure:
            raise self.failure
        return ProviderEvidence(
            {"choices": [{"finish_reason": "stop", "message": {"content": self.content}}]},
            TokenUsage(input_tokens=3, output_tokens=2),
            self.cost,
            "actual-fixture",
            None,
            "gen-fixture",
        )


def configured(tmp_path, *, live=True, provider=None, mutate=None):
    data = synthetic_config()
    # Ordinary functional tests start multiple covered key-free children. Their
    # total startup budget is independent of deliberate timeout regressions,
    # which set their own short limits explicitly.
    data["aliases"]["summary"]["limits"].update(
        attempt_timeout_seconds=5, request_timeout_seconds=20
    )
    if live:
        data.update(
            mode="live",
            access={"owner_only_ipc": True},
            budgets={
                "call": {"scope": "project", "period": "call", "cap_usd": "1"},
                "run": {"scope": "project", "period": "run", "cap_usd": "1"},
            },
        )
        data["providers"]["router"]["credential"] = {"env_var": "FIXTURE_KEY"}
        data["aliases"]["summary"].update(
            budget_policy_refs=["call", "run"],
            pricing={
                "revision": "reviewed-fixture",
                "bound_source": "synthetic-reviewed",
                "input_bound": "utf8_bytes_with_framing",
                "framing_tokens_per_item": 64,
                "input_usd_per_token": "0.000001",
                "output_usd_per_token": "0.000001",
            },
        )
    else:
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir(exist_ok=True)
        (fixtures / "first.txt").write_text("first")
        (fixtures / "second.txt").write_text("second")
        data["fake"] = {
            "fixture_root": str(fixtures),
            "responses": {"summary": {"default": ["first.txt", "second.txt"]}},
        }
    if mutate:
        mutate(data)
    snapshot = load_project_config(write_config(tmp_path, data), check_credentials=False)
    engine = Engine(StateStore(tmp_path / "state"), provider=provider or ProviderDouble())
    engine.registry.activate(snapshot)
    authority = Authority(
        "project-a",
        "owner",
        PrincipalConfig(
            token_file="/unused-test-token",
            aliases=["summary"],
            operations=["chat"],
            permissions=["infer", "receipts:own"],
            run_id="run-one",
        ),
    )
    return engine, authority, snapshot


def test_success_receipt_and_metadata_only_replay_never_redispatch(tmp_path):
    provider = ProviderDouble()
    engine, authority, _ = configured(tmp_path, provider=provider)
    result = engine.run(authority, request())
    assert result.content == "ok" and result.execution_status == "completed"
    assert result.receipts[0].measured_usd == Decimal("0.0001")
    assert result.model.actual_model == "actual-fixture"
    replay = engine.run(authority, request())
    assert replay.output_status == "unavailable" and replay.receipts == result.receipts
    assert replay.provider_metadata["result_available"] is False
    assert len(provider.calls) == 1


def test_opt_in_cache_is_project_principal_and_snapshot_scoped(tmp_path):
    def mutate(data):
        data["evidence_policy"] = {
            "retain_results": True,
            "result_ttl_seconds": 60,
            "cache_enabled": True,
        }

    provider = ProviderDouble()
    engine, authority, snapshot = configured(tmp_path, provider=provider, mutate=mutate)
    first = engine.run(authority, request())
    assert engine.run(authority, request()) == first
    reused = engine.run(authority, request("two"))
    assert reused.content == first.content
    assert reused.receipts[0].evidence_status == "cached_reuse"
    assert reused.receipts[0].protected_debit_usd == 0
    assert reused.provider_metadata["reused_receipt_ids"] == [first.receipts[0].receipt_id]
    other = Authority(authority.project, "other", authority.grants)
    engine.run(other, request("three"))
    assert len(provider.calls) == 2
    assert (
        engine.results.get(
            authority.project,
            authority.principal,
            "one",
            now=datetime.now(UTC) + timedelta(seconds=61),
        )
        is None
    )
    assert engine.ledger.request(authority.project, authority.principal, "one")


def test_fake_sequences_are_captured_durable_and_never_use_provider(tmp_path):
    class Forbidden:
        def invoke(self, *args, **kwargs):
            raise AssertionError("fake mode used provider")

    engine, authority, _ = configured(tmp_path, live=False, provider=Forbidden())
    (tmp_path / "fixtures" / "first.txt").write_text("mutated")
    first = engine.run(authority, request())
    assert first.content == "first" and first.receipts[0].evidence_status == "simulated"
    assert engine.run(authority, request()).output_status == "unavailable"
    assert engine.run(authority, request("two")).content == "second"
    exhausted = engine.run(authority, request("three"))
    assert exhausted.output_status == "provider_failed"
    assert exhausted.receipts[0].measured_usd is None
    assert engine.ledger.report("project-a")["protected_debit_usd"] == "0"


def test_invalid_document_preserves_measured_charge(tmp_path):
    engine, authority, _ = configured(
        tmp_path,
        provider=ProviderDouble(content="not-json"),
        mutate=lambda d: d["aliases"]["summary"].update(capabilities=["plain_text", "json_object"]),
    )
    req = request(
        input={
            "kind": "chat",
            "messages": [{"role": "user", "content": "public"}],
            "response_format": "json_object",
        }
    )
    result = engine.run(authority, req)
    assert result.output_status == "invalid_document"
    assert result.receipts[0].measured_usd == Decimal("0.0001")


def test_unknown_charge_blocks_strict_scopes_and_bounded_retry_retains_each_ceiling(tmp_path):
    double = ProviderDouble(failure=ProviderFailure("provider_failed", dispatched=True))
    engine, authority, _ = configured(tmp_path, provider=double)
    result = engine.run(authority, request())
    assert result.receipts[0].reconciliation_required
    with pytest.raises(EngineError, match="unknown_billing_hold"):
        engine.run(authority, request("two"))
    assert len(double.calls) == 1
    other = tmp_path / "bounded"
    other.mkdir()
    engine, authority, _ = configured(
        other,
        provider=double,
        mutate=lambda d: d["aliases"]["summary"].update(
            attempt_policy={"max_attempts": 3, "unknown_charge_policy": "bounded_exposure"}
        ),
    )
    result = engine.run(authority, request())
    assert len(result.receipts) == 3
    assert len(double.calls) == 4
    assert all(r.measured_usd is None and r.protected_debit_usd > 0 for r in result.receipts)


def test_valid_output_can_have_unknown_billing_and_estimated_evidence_is_labelled(tmp_path):
    engine, authority, _ = configured(tmp_path, provider=ProviderDouble(cost=None))
    result = engine.run(authority, request())
    assert result.output_status == "valid"
    assert result.receipts[0].evidence_status == "estimated"
    assert result.receipts[0].measured_usd is None
    assert result.receipts[0].estimated_usd == Decimal("0.000005")


def test_grants_and_run_binding_fail_before_any_admission(tmp_path):
    engine, authority, _ = configured(tmp_path)
    bound = Authority(
        authority.project,
        authority.principal,
        authority.grants.model_copy(update={"run_id": "allowed"}),
    )
    with pytest.raises(EngineError, match="run_binding"):
        engine.run(bound, request())
    assert engine.ledger.report("project-a")["attempts"] == 0


def test_persistence_failure_after_inference_fences_success_and_preserves_reserve(
    tmp_path, monkeypatch
):
    engine, authority, _ = configured(tmp_path)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("injected persistence failure")

    monkeypatch.setattr(engine.ledger, "settle", fail)
    with pytest.raises(EngineError, match="ledger_unavailable_query_receipt"):
        engine.run(authority, request())
    assert engine.fenced
    attempt = engine.ledger.attempts("project-a", "owner", "one")[0]
    assert attempt["state"] == "dispatch_intent" and Decimal(attempt["protected_usd"]) > 0
    assert engine.ledger.recover() == 1
    assert engine.receipts(authority, "one")[0].reconciliation_required


def test_trace_export_is_metadata_only_retryable_and_private(tmp_path):
    engine, authority, _ = configured(tmp_path)
    engine.run(authority, request())
    path = tmp_path / "trace.jsonl"
    count = export_trace(engine.store, "project-a", path)
    assert count >= 5 and export_trace(engine.store, "project-a", path) == 0
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert len({e["event_id"] for e in events}) == count
    assert "public" not in path.read_text()
    assert path.stat().st_mode & 0o077 == 0


def test_alias_queue_cancellation_does_not_create_second_attempt(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    class Blocking(ProviderDouble):
        def __init__(self):
            super().__init__()
            self.started, self.release = threading.Event(), threading.Event()

        def invoke(self, *args, **kwargs):
            self.started.set()
            assert self.release.wait(10)
            return super().invoke(*args, **kwargs)

    provider = Blocking()
    engine, authority, _ = configured(tmp_path, provider=provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(engine.run, authority, request())
        assert provider.started.wait(5)
        second = pool.submit(engine.run, authority, request("two"))
        # Wait for durable registration, without depending on a fixed race delay.
        for _ in range(100):
            try:
                engine.ledger.request("project-a", "owner", "two")
                break
            except ValueError:
                threading.Event().wait(0.005)
        engine.cancel(authority, "two")
        with pytest.raises(EngineError, match="cancelled"):
            second.result(timeout=5)
        provider.release.set()
        assert first.result(timeout=5).output_status == "valid"
    assert len(provider.calls) == 1
    assert engine.ledger.attempts("project-a", "owner", "two") == []


def test_v1_to_current_migration_preserves_money_and_quotas(tmp_path):
    from llm_gateway.storage.sqlite import SCHEMA, SCHEMA_VERSION

    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    path = root / "gateway.sqlite"
    path.touch(mode=0o600)
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA + "PRAGMA user_version=1;")
        db.execute("INSERT INTO runtime_flags VALUES ('migration-fixture','retained')")
    store = StateStore(root)
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            db.execute("SELECT value FROM runtime_flags WHERE name='migration-fixture'").fetchone()[
                0
            ]
            == "retained"
        )
        assert db.execute("SELECT COUNT(*) FROM retained_results").fetchone()[0] == 0


def test_submission_deadline_and_early_cancellation_precede_provider_access(tmp_path):
    import threading

    engine, authority, _ = configured(
        tmp_path,
        mutate=lambda data: data["aliases"]["summary"]["limits"].update(
            attempt_timeout_seconds=1, request_timeout_seconds=2
        ),
    )
    with pytest.raises(EngineError, match="deadline"):
        engine.run(authority, request(), received_at=datetime.now(UTC) - timedelta(seconds=3))
    signal = threading.Event()
    signal.set()
    with pytest.raises(EngineError, match="cancelled"):
        engine.run(authority, request("two"), cancel_signal=signal)
    assert not engine.provider.calls
    assert engine.ledger.request("project-a", "owner", "one")["execution_status"] == "timed_out"
    assert engine.ledger.request("project-a", "owner", "two")["execution_status"] == "cancelled"
