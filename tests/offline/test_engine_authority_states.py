"""Authority, cancellation and explicit retry transitions before/after admission."""

import copy
from decimal import Decimal

import pytest
from test_engine import ProviderDouble, configured
from test_ledger import request

from llm_gateway.providers.runtime import ProviderFailure
from llm_gateway.service.engine import Authority, Engine, EngineError
from llm_gateway.storage import StateStore
from llm_gateway.storage.ledger import LedgerError

pytestmark = pytest.mark.offline


@pytest.mark.parametrize("missing", ["permissions", "aliases", "operations"])
def test_each_inference_grant_is_required_before_recording_an_attempt(tmp_path, missing):
    engine, authority, _ = configured(tmp_path)
    limited = Authority(
        authority.project, authority.principal, authority.grants.model_copy(update={missing: []})
    )
    with pytest.raises(EngineError, match="inference_forbidden"):
        engine.run(limited, request())
    assert engine.provider.calls == [] and engine.ledger.report(authority.project)["attempts"] == 0


def test_phase_binding_rejects_explicit_conflict_and_supplies_omitted_phase(tmp_path):
    engine, authority, _ = configured(tmp_path)
    bound = Authority(
        authority.project,
        authority.principal,
        authority.grants.model_copy(update={"phase": "training"}),
    )
    explicit = request(context={"run_id": "run-one", "phase": "task"})
    with pytest.raises(EngineError, match="phase_binding_mismatch"):
        engine.run(bound, explicit)
    assert engine.provider.calls == []
    result = engine.run(bound, request("implicit"))
    assert result.output_status == "valid"
    import json

    assert (
        json.loads(
            engine.ledger.request(authority.project, authority.principal, "implicit")[
                "context_json"
            ]
        )["phase"]
        == "training"
    )


@pytest.mark.parametrize("binding", ["run_id", "phase"])
def test_changed_bound_scope_cannot_read_previous_principal_history(tmp_path, binding):
    engine, authority, _ = configured(tmp_path)
    engine.run(authority, request())
    before = engine.ledger.report(authority.project)
    limited = Authority(
        authority.project,
        authority.principal,
        authority.grants.model_copy(update={binding: "different"}),
    )
    with pytest.raises(LedgerError, match="request_unknown"):
        engine.lookup(limited, "one")
    assert engine.ledger.report(authority.project) == before
    assert len(engine.provider.calls) == 1


def test_already_fenced_service_rejects_new_work_without_inflight_leaks(tmp_path):
    engine, authority, _ = configured(tmp_path)
    engine.fenced = True
    with pytest.raises(EngineError, match="ledger_unavailable"):
        engine.run(authority, request())
    assert engine.inflight == 0 and engine.signals == {} and engine.provider.calls == []


def test_stale_authenticated_config_digest_cannot_dispatch_a_different_route(tmp_path):
    engine, authority, _ = configured(tmp_path)
    stale = Authority(authority.project, authority.principal, authority.grants, "0" * 64)
    with pytest.raises(EngineError, match="configuration_changed_before_admission"):
        engine.run(stale, request())
    assert engine.provider.calls == []
    assert engine.run(authority, request()).output_status == "valid"
    assert len(engine.provider.calls) == 1


def test_cancellation_racing_registration_leaves_no_attempt_or_reservation(tmp_path, monkeypatch):
    engine, authority, _ = configured(tmp_path)
    original = engine.ledger.register

    def register(*args, **kwargs):
        row, created = original(*args, **kwargs)
        engine.ledger.cancel(authority.project, authority.principal, "one")
        return row, created

    monkeypatch.setattr(engine.ledger, "register", register)
    with pytest.raises(EngineError, match="cancelled_or_deadline"):
        engine.run(authority, request())
    assert (
        engine.provider.calls == []
        and engine.ledger.attempts(authority.project, authority.principal, "one") == []
    )
    assert (
        engine.ledger.request(authority.project, authority.principal, "one")["execution_status"]
        == "cancelled"
    )
    assert engine.total == 0 and engine.waiting == 0 and engine.events == {}


def test_declared_fallback_preserves_each_route_and_observed_charge(tmp_path):
    class Routes(ProviderDouble):
        def invoke(self, prepared, provider, **kwargs):
            self.content = "not-json" if not self.calls else "{}"
            return super().invoke(prepared, provider, **kwargs)

    provider = Routes()

    def mutate(data):
        data["providers"]["secondary"] = {
            "kind": "openrouter",
            "credential": {"env_var": "OTHER_SYNTHETIC_KEY"},
        }
        profile = data["aliases"]["summary"]
        # Two attempts each start key-free preparation/output workers. Give
        # covered Linux child startup room; deadline semantics have other tests.
        profile["limits"].update(attempt_timeout_seconds=5, request_timeout_seconds=20)
        profile["capabilities"] = ["plain_text", "json_object"]
        profile["attempt_policy"] = {
            "max_attempts": 2,
            "fallback_routes": [
                {
                    "provider_ref": "secondary",
                    "upstream_model": "synthetic/fallback",
                    "capabilities": list(profile["capabilities"]),
                    "pricing": copy.deepcopy(profile["pricing"]),
                }
            ],
        }

    engine, authority, _ = configured(tmp_path, provider=provider, mutate=mutate)
    req = request(
        input={
            "kind": "chat",
            "messages": [{"role": "user", "content": "public"}],
            "response_format": "json_object",
        }
    )
    result = engine.run(authority, req)
    assert result.output_status == "valid" and len(result.receipts) == 2
    assert [r.model.provider_ref for r in result.receipts] == ["router", "secondary"]
    assert [r.model.upstream_model for r in result.receipts] == [
        "fixture-model",
        "synthetic/fallback",
    ]
    assert all(r.model.configured_model == "fixture-model" for r in result.receipts)
    assert all(r.measured_usd == Decimal("0.0001") for r in result.receipts)
    assert engine.ledger.report(authority.project)["measured_usd"] == "0.0002"


@pytest.mark.parametrize("code", ["provider_cancelled", "provider_deadline_exceeded"])
def test_provider_deadline_or_cancel_does_not_buy_another_declared_retry(tmp_path, code):
    provider = ProviderDouble(failure=ProviderFailure(code, dispatched=False))
    engine, authority, _ = configured(
        tmp_path,
        provider=provider,
        mutate=lambda data: data["aliases"]["summary"].update(attempt_policy={"max_attempts": 3}),
    )
    result = engine.run(authority, request())
    assert len(provider.calls) == len(result.receipts) == 1
    assert result.receipts[0].evidence_status == "not_dispatched"
    assert result.receipts[0].protected_debit_usd == 0


def test_free_policy_without_returned_cost_does_not_fabricate_measured_zero(tmp_path):
    def mutate(data):
        for budget in data["budgets"].values():
            budget["cap_usd"] = "0"
        profile = data["aliases"]["summary"]
        profile.update(
            upstream_model="openrouter/free",
            pricing={
                "revision": "synthetic-free",
                "bound_source": "synthetic-free-only",
                "free_only": True,
            },
        )

    engine, authority, _ = configured(tmp_path, provider=ProviderDouble(cost=None), mutate=mutate)
    receipt = engine.run(authority, request()).receipts[0]
    assert receipt.evidence_status == "explicit_free" and receipt.measured_usd is None
    assert receipt.estimated_usd is None and receipt.protected_debit_usd == 0


@pytest.mark.parametrize("capacity", [{"max_queue": 0}, {"max_concurrency": 65}])
def test_invalid_engine_capacity_fails_before_dispatch(tmp_path, capacity):
    with pytest.raises(EngineError, match="engine_capacity_invalid"):
        Engine(StateStore(tmp_path / "state"), **capacity)


def test_explicit_client_run_grant_creates_distinct_run_buckets_and_requires_identity(tmp_path):
    engine, authority, _ = configured(tmp_path)
    dynamic = Authority(
        authority.project,
        authority.principal,
        authority.grants.model_copy(update={"run_id": None, "allow_client_run_ids": True}),
    )
    first = engine.run(dynamic, request("first", context={"run_id": "job-one"}))
    second = engine.run(dynamic, request("second", context={"run_id": "job-two"}))
    assert first.output_status == second.output_status == "valid"
    with engine.store.connection() as connection:
        count = connection.execute(
            "SELECT count(*) FROM budget_buckets WHERE policy_id='run'"
        ).fetchone()[0]
    assert count == 2
    with pytest.raises(EngineError, match="paid_run_binding_required"):
        engine.run(dynamic, request("missing", context={}))
