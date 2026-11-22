"""COST-01–17: real SQLite races, retained exposure and crash recovery."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from test_config import synthetic_config, write_config

from llm_gateway.config import load_project_config
from llm_gateway.config.models import BudgetPolicy
from llm_gateway.config.registry import ConfigRegistry
from llm_gateway.contracts import InferenceRequest, TokenUsage
from llm_gateway.storage import StateStore
from llm_gateway.storage.buckets import bind_bucket
from llm_gateway.storage.ledger import Ledger, LedgerError

pytestmark = pytest.mark.offline


@pytest.fixture
def setup(tmp_path):
    store = StateStore(tmp_path / "state")
    snapshot = load_project_config(write_config(tmp_path, synthetic_config()))
    ConfigRegistry(store).activate(snapshot)
    return Ledger(store), snapshot


def request(identifier="one", **changes):
    return InferenceRequest.model_validate(
        dict(
            request_id=identifier,
            alias="summary",
            operation="chat",
            input={"kind": "chat", "messages": [{"role": "user", "content": "public"}]},
            context={"run_id": "run-one"},
        )
        | changes
    )


def bucket(req, *, cap="1", period="day", policy_id="daily"):
    return bind_bucket(
        policy_id,
        BudgetPolicy(scope="project", period=period, cap_usd=cap),
        project_id="project-a",
        principal_id="owner",
        request_id=req.request_id,
        provider_ref="router",
        alias="summary",
        context=req.context,
        timezone="UTC",
        now=datetime.now(UTC),
    )


def admitted(setup, req=None, *, reserve="0.4", buckets=None):
    ledger, snapshot = setup
    req = req or request()
    ledger.register("project-a", "owner", req, snapshot)
    attempt = ledger.admit(
        "project-a",
        "owner",
        req.request_id,
        ordinal=1,
        route={"provider_ref": "router"},
        reserve=Decimal(reserve),
        buckets=buckets or [bucket(req)],
        max_attempts=1,
    )
    return attempt


def test_scoped_usage_cannot_reveal_another_callers_shared_bucket_balance(setup):
    ledger, snapshot = setup
    own = request("own", context={"run_id": "own-run"})
    other = request("other", context={"run_id": "other-run"})
    for principal, req, amount in [
        ("owner", own, "0.1"),
        ("different-principal", other, "0.2"),
    ]:
        ledger.register("project-a", principal, req, snapshot)
        attempt = ledger.admit(
            "project-a",
            principal,
            req.request_id,
            ordinal=1,
            route={"provider_ref": "router"},
            reserve=Decimal("0.3"),
            buckets=[bucket(req)],
            max_attempts=1,
        )
        ledger.dispatch_intent("project-a", attempt)
        ledger.settle("project-a", attempt, status="measured", measured=Decimal(amount))
    own_report = ledger.report("project-a", principal="owner")
    assert own_report["measured_usd"] == "0.1" and own_report["attempts"] == 1
    assert own_report["buckets"] == []
    filtered = ledger.report("project-a", filters={"run_id": "own-run"})
    assert filtered["measured_usd"] == "0.1" and filtered["buckets"] == []
    all_usage = ledger.report("project-a")
    assert all_usage["measured_usd"] == "0.3"
    assert len(all_usage["buckets"]) == 1
    assert all_usage["buckets"][0]["protected_debit_usd"] == "0.3"


@pytest.mark.parametrize(
    "kind", ["ordinal_zero", "ordinal_too_large", "duplicate", "foreign", "no_paid_cap"]
)
def test_invalid_admission_identity_never_allocates_money(setup, kind):
    ledger, snapshot = setup
    req = request()
    ledger.register("project-a", "owner", req, snapshot)
    buckets = [bucket(req)]
    ordinal = 1
    if kind == "ordinal_zero":
        ordinal = 0
    elif kind == "ordinal_too_large":
        ordinal = 11
    elif kind == "duplicate":
        buckets *= 2
    elif kind == "foreign":
        buckets = [replace(buckets[0], project_id="other-project")]
    else:
        buckets = []
    with pytest.raises(LedgerError):
        ledger.admit(
            "project-a",
            "owner",
            req.request_id,
            ordinal=ordinal,
            route={},
            reserve=Decimal("0.1"),
            buckets=buckets,
            max_attempts=10,
        )
    assert ledger.report("project-a")["protected_debit_usd"] == "0"
    assert ledger.attempts("project-a", "owner", req.request_id) == []


def test_pending_attempt_cannot_be_billed_finished_or_hidden_by_next_attempt(setup):
    ledger, _ = setup
    req = request(limits={"max_attempts": 1})
    attempt = admitted(setup, req)
    with pytest.raises(LedgerError, match="settlement_without_intent"):
        ledger.settle("project-a", attempt, status="measured", measured=Decimal("0.1"))
    with pytest.raises(LedgerError, match="attempt_not_settled"):
        ledger.finish(
            "project-a", "owner", "one", execution_status="completed", output_status="valid"
        )
    with pytest.raises(LedgerError, match="attempt_sequence_conflict"):
        ledger.admit(
            "project-a",
            "owner",
            "one",
            ordinal=2,
            route={},
            reserve=Decimal("0.1"),
            buckets=[bucket(req)],
            max_attempts=2,
        )
    assert ledger.report("project-a")["protected_debit_usd"] == "0.4"
    ledger.settle("project-a", attempt, status="no_charge")
    with pytest.raises(LedgerError, match="attempt_limit"):
        ledger.admit(
            "project-a",
            "owner",
            "one",
            ordinal=2,
            route={},
            reserve=Decimal("0.1"),
            buckets=[bucket(req)],
            max_attempts=2,
        )
    assert len(ledger.attempts("project-a", "owner", "one")) == 1


def test_caller_total_ceiling_includes_already_billed_attempts(setup):
    ledger, _ = setup
    req = request(limits={"max_attempts": 2, "max_usd": "0.2"})
    attempt = admitted(setup, req, reserve="0.1")
    ledger.dispatch_intent("project-a", attempt)
    ledger.settle("project-a", attempt, status="measured", measured=Decimal("0.1"))
    with pytest.raises(LedgerError, match="caller_budget_exceeded"):
        ledger.admit(
            "project-a",
            "owner",
            "one",
            ordinal=2,
            route={},
            reserve=Decimal("0.2"),
            buckets=[bucket(req)],
            max_attempts=2,
        )
    assert ledger.report("project-a")["protected_debit_usd"] == "0.1"


def test_expired_request_cannot_allocate_a_fresh_attempt(setup):
    ledger, snapshot = setup
    req = request()
    ledger.register(
        "project-a", "owner", req, snapshot, now=datetime.now(UTC) - timedelta(seconds=10)
    )
    with pytest.raises(LedgerError, match="request_deadline_exceeded"):
        admitted(setup, req)
    assert ledger.report("project-a")["attempts"] == 0


@pytest.mark.parametrize(
    "evidence",
    [
        {"status": "unsupported"},
        {"status": "unavailable", "measured": Decimal("0.1")},
        {"status": "measured", "measured": Decimal("0.1"), "estimated": Decimal("0.1")},
    ],
)
def test_conflicting_evidence_preserves_the_durable_intent_and_reserve(setup, evidence):
    ledger, _ = setup
    attempt = admitted(setup)
    ledger.dispatch_intent("project-a", attempt)
    with pytest.raises(LedgerError):
        ledger.settle("project-a", attempt, **evidence)
    assert ledger.attempts("project-a", "owner", "one")[0]["state"] == "dispatch_intent"
    assert ledger.report("project-a")["protected_debit_usd"] == "0.4"


def test_unexpected_charge_on_zero_reserved_route_is_recorded_and_fences_project(setup):
    ledger, _ = setup
    attempt = admitted(setup, reserve="0")
    ledger.dispatch_intent("project-a", attempt)
    ledger.settle("project-a", attempt, status="measured", measured=Decimal("0.01"))
    report = ledger.report("project-a")
    assert report["measured_usd"] == "0.01" and report["protected_debit_usd"] == "0.01"
    assert report["overrun"] is True
    with pytest.raises(LedgerError, match="admission_fenced"):
        admitted(setup, request("two"), reserve="0")
    assert ledger.report("project-a")["attempts"] == 1


def test_simulated_or_pending_receipts_cannot_be_relabelled_paid(setup):
    ledger, _ = setup
    attempt = admitted(setup)
    evidence = dict(
        attempt_id=attempt,
        request_id="one",
        generation_id="generation",
        measured=Decimal("0.1"),
        authority="synthetic trusted operator",
        observed_at=datetime.now(UTC),
    )
    with pytest.raises(LedgerError, match="evidence_identity_mismatch"):
        ledger.reconcile("project-a", "pending", **evidence)
    ledger.settle("project-a", attempt, status="simulated")
    with pytest.raises(LedgerError, match="evidence_reconciliation_forbidden"):
        ledger.reconcile("project-a", "simulation", **evidence)
    assert ledger.report("project-a")["measured_usd"] == "0"


@pytest.mark.parametrize("change", ["authority", "timestamp", "identity_length"])
def test_reconciliation_requires_bounded_identity_and_aware_operator_attestation(setup, change):
    ledger, _ = setup
    attempt = admitted(setup)
    evidence = dict(
        attempt_id=attempt,
        request_id="one",
        generation_id="generation",
        measured=Decimal("0.1"),
        authority="synthetic operator",
        observed_at=datetime.now(UTC),
    )
    evidence_id = "evidence"
    if change == "authority":
        evidence["authority"] = ""
    elif change == "timestamp":
        evidence["observed_at"] = datetime(2026, 11, 11)
    else:
        evidence_id = "e" * 129
    with pytest.raises(LedgerError, match="evidence_invalid"):
        ledger.reconcile("project-a", evidence_id, **evidence)
    assert ledger.report("project-a")["protected_debit_usd"] == "0.4"


def test_invalid_terminal_status_unknown_cancel_and_cross_project_snapshot_are_denied(setup):
    ledger, snapshot = setup
    with pytest.raises(LedgerError, match="execution_status_invalid"):
        ledger.finish(
            "project-a", "owner", "missing", execution_status="in_progress", output_status="pending"
        )
    with pytest.raises(LedgerError, match="request_unknown"):
        ledger.cancel("project-a", "owner", "missing")
    with pytest.raises(LedgerError, match="project_snapshot_mismatch"):
        ledger.register("different-project", "owner", request(), snapshot)
    with pytest.raises(LedgerError, match="alias_operation_forbidden"):
        ledger.register("project-a", "owner", request(alias="unknown"), snapshot)
    # Duplicate terminal cleanup is idempotent and emits no invented request.
    ledger.finish(
        "project-a", "owner", "missing", execution_status="failed", output_status="unavailable"
    )
    assert ledger.report("project-a")["attempts"] == 0


def test_idempotency_race_and_reload_keeps_old_snapshot(setup, tmp_path):
    ledger, snapshot = setup
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(
            pool.map(lambda _: ledger.register("project-a", "owner", request(), snapshot), range(8))
        )
    assert sum(created for _, created in records) == 1
    assert len({row["trace_id"] for row, _ in records}) == 1
    changed = load_project_config(write_config(tmp_path, synthetic_config(model="new")))
    ConfigRegistry(ledger.store).activate(changed)
    row, created = ledger.register("project-a", "owner", request(), changed)
    assert not created and row["config_digest"] == snapshot.digest
    with pytest.raises(LedgerError, match="idempotency_conflict"):
        ledger.register("project-a", "owner", request(context={"run_id": "different"}), changed)
    with pytest.raises(LedgerError, match="unknown"):
        ledger.request("project-a", "other", "one")


def test_atomic_conjunctive_budgets_and_race(setup):
    ledger, snapshot = setup
    for i in range(8):
        ledger.register("project-a", "owner", request(str(i)), snapshot)

    def try_admit(i):
        try:
            return admitted(setup, request(str(i)))
        except LedgerError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(try_admit, range(8)))
    assert results.count("budget_exceeded") == 6
    assert ledger.report("project-a")["protected_debit_usd"] == "0.8"
    # A second cap fails; no partial allocation remains in a newly created bucket.
    req = request("tight")
    with pytest.raises(LedgerError, match="budget_exceeded"):
        admitted(
            setup,
            req,
            reserve="0.1",
            buckets=[bucket(req, policy_id="new"), bucket(req, cap="0.01", policy_id="tight")],
        )
    with ledger.store.connection() as db:
        assert db.execute("SELECT 1 FROM budget_buckets WHERE policy_id='new'").fetchone() is None


def test_dispatch_single_winner_and_quota_is_consumed_durably(setup):
    ledger, _ = setup
    attempt = admitted(setup)

    def claim(_):
        try:
            ledger.dispatch_intent("project-a", attempt, quota=("native-x", "day-fixture"))
            return True
        except LedgerError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(4))) == 1
    ledger.settle("project-a", attempt, status="unavailable")
    second = admitted(setup, request("second"), reserve="0")
    with pytest.raises(LedgerError, match="quota_consumed"):
        ledger.dispatch_intent("project-a", second, quota=("native-x", "day-fixture"))


@pytest.mark.parametrize(
    "status,expected,contingent,hold",
    [
        ("measured", "0.1", "0", False),
        ("estimated", "0.4", "0.3", False),
        ("unavailable", "0.4", "0.4", True),
    ],
)
def test_settlement_independent_from_invalid_output(setup, status, expected, contingent, hold):
    ledger, _ = setup
    attempt = admitted(setup)
    ledger.dispatch_intent("project-a", attempt)
    kwargs = (
        {"measured": Decimal("0.1")}
        if status == "measured"
        else {"estimated": Decimal("0.1")}
        if status == "estimated"
        else {}
    )
    ledger.settle("project-a", attempt, status=status, output_status="invalid_document", **kwargs)
    report = ledger.report("project-a")
    assert report["protected_debit_usd"] == expected
    assert report["contingent_exposure_usd"] == contingent
    assert bool(ledger.attempts("project-a", "owner", "one")[0]["unknown_hold"]) == hold
    if hold:
        with pytest.raises(LedgerError, match="unknown_billing_hold"):
            admitted(setup, request("other"), reserve="0.01")
    with pytest.raises(LedgerError, match="already_settled"):
        ledger.settle("project-a", attempt, status=status, **kwargs)


def test_reconciliation_is_idempotent_and_conflicting_evidence_is_rejected(setup):
    ledger, _ = setup
    attempt = admitted(setup)
    ledger.dispatch_intent("project-a", attempt)
    ledger.settle(
        "project-a",
        attempt,
        status="estimated",
        estimated=Decimal("0.1"),
        generation_id="gen-fixture",
        usage=TokenUsage(input_tokens=3, output_tokens=1),
    )
    evidence = dict(
        attempt_id=attempt,
        request_id="one",
        generation_id="gen-fixture",
        measured=Decimal("0.07"),
        authority="verified-public-fixture",
        observed_at=datetime.now(UTC),
    )
    assert ledger.reconcile("project-a", "evidence", **evidence)
    assert not ledger.reconcile("project-a", "evidence", **evidence)
    assert ledger.report("project-a")["protected_debit_usd"] == "0.07"
    with pytest.raises(LedgerError, match="evidence_id_conflict"):
        ledger.reconcile("project-a", "evidence", **(evidence | {"measured": Decimal("0.08")}))
    with pytest.raises(LedgerError, match="measured_evidence_conflict"):
        ledger.reconcile("project-a", "new-evidence", **(evidence | {"measured": Decimal("0.08")}))
    with pytest.raises(LedgerError, match="attempt_unknown"):
        ledger.reconcile("other-project", "evidence", **evidence)


def test_recovery_releases_only_proven_undispatched_and_never_retries(setup):
    ledger, _ = setup
    admitted(setup)
    attempt = admitted(setup, request("two"))
    ledger.dispatch_intent("project-a", attempt)
    assert Ledger(StateStore(ledger.store.root)).recover() == 2
    assert ledger.recover() == 0
    report = ledger.report("project-a")
    assert report["protected_debit_usd"] == "0.4"
    assert report["unknown_attempts"] == 1
    assert ledger.request("project-a", "owner", "one")["execution_status"] == "failed"
    with pytest.raises(LedgerError, match="not_dispatchable"):
        ledger.admit(
            "project-a",
            "owner",
            "two",
            ordinal=2,
            route={},
            reserve=Decimal("0"),
            buckets=[],
            max_attempts=2,
        )


def test_original_bucket_and_overrun_remain_protected(setup):
    ledger, _ = setup
    attempt = admitted(setup)
    ledger.dispatch_intent("project-a", attempt)
    ledger.settle("project-a", attempt, status="measured", measured=Decimal("0.5"))
    assert ledger.report("project-a")["overrun"]
    with pytest.raises(LedgerError, match="hold"):
        admitted(setup, request("later"), reserve="0.01")


def test_cancel_and_fence_prevent_new_dispatch(setup):
    ledger, _ = setup
    attempt = admitted(setup)
    ledger.cancel("project-a", "owner", "one")
    with pytest.raises(LedgerError, match="not_dispatchable"):
        ledger.dispatch_intent("project-a", attempt)
    with ledger.store.transaction() as db:
        db.execute("INSERT INTO runtime_flags VALUES ('admission_fenced','restore')")
    with pytest.raises(LedgerError, match="admission_fenced"):
        admitted(setup, request("two"))


def test_report_counts_attempt_once_not_each_budget_and_preserves_precision(setup):
    ledger, _ = setup
    req = request()
    amount = "0.400000000000000001"
    attempt = admitted(
        setup,
        reserve=amount,
        buckets=[bucket(req), bucket(req, policy_id="monthly", period="month")],
    )
    ledger.dispatch_intent("project-a", attempt)
    with localcontext() as context:
        context.prec = 3  # Host caller precision must not corrupt accounting.
        ledger.settle(
            "project-a", attempt, status="measured", measured=Decimal("0.100000000000000001")
        )
        report = ledger.report("project-a", group_by="run_id")
    assert report["measured_usd"] == "0.100000000000000001"
    assert report["attempts"] == 1
    assert report["groups"]["run-one"]["measured_usd"] == report["measured_usd"]
    assert ledger.report("project-a", principal="other")["attempts"] == 0
    assert ledger.report("project-a", filters={"run_id": "no-match"})["attempts"] == 0
    assert len(report["buckets"]) == 2
    assert all(b["remaining_usd"] == "0.899999999999999999" for b in report["buckets"])


def test_failed_settlement_rolls_back_money_and_trace(setup, monkeypatch):
    import llm_gateway.storage.ledger as module

    ledger, _ = setup
    attempt = admitted(setup)
    ledger.dispatch_intent("project-a", attempt)

    def fail(*args, **kwargs):
        raise OSError("injected disk failure")

    monkeypatch.setattr(module, "_event", fail)
    with pytest.raises(OSError):
        ledger.settle("project-a", attempt, status="measured", measured=Decimal("0.1"))
    row = ledger.attempts("project-a", "owner", "one")[0]
    assert row["state"] == "dispatch_intent"
    assert Decimal(row["protected_usd"]) == Decimal("0.4")
    assert row["measured_usd"] is None


def test_real_process_death_after_durable_intent_is_unknown(setup):
    import os
    import subprocess
    import sys
    from pathlib import Path

    ledger, _ = setup
    attempt = admitted(setup)
    script = """
import os, sys
from pathlib import Path
from llm_gateway.storage import StateStore
from llm_gateway.storage.ledger import Ledger
Ledger(StateStore(Path(sys.argv[1]))).dispatch_intent('project-a', sys.argv[2])
os._exit(73)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(ledger.store.root), attempt],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    )
    assert result.returncode == 73
    assert ledger.recover() == 1
    row = ledger.attempts("project-a", "owner", "one")[0]
    assert row["unknown_hold"] and row["measured_usd"] is None
    assert Decimal(row["protected_usd"]) == Decimal("0.4")


def test_usage_time_windows_local_day_groups_and_unsettled_exposure(setup):
    ledger, _ = setup
    first = admitted(setup, request("earlier"))
    ledger.dispatch_intent("project-a", first)
    ledger.settle("project-a", first, status="measured", measured=Decimal("0.1"))
    admitted(setup, request("later"))
    with ledger.store.transaction() as connection:
        connection.execute(
            "UPDATE requests SET created_at=? WHERE request_id=?",
            ("2026-11-21T22:30:00+00:00", "earlier"),
        )
        connection.execute(
            "UPDATE requests SET created_at=? WHERE request_id=?",
            ("2026-11-21T23:30:00+00:00", "later"),
        )
    result = ledger.report("project-a", group_by="day", timezone="Europe/Berlin")
    assert set(result["groups"]) == {"2026-11-21", "2026-11-22"}
    assert result["currency"] == "USD" and result["timezone"] == "Europe/Berlin"
    assert result["measured_attempts"] == 1 and result["unsettled_attempts"] == 1
    assert result["measured_usd"] == "0.1" and result["contingent_exposure_usd"] == "0.4"
    selected = ledger.report(
        "project-a", filters={"since": "2026-11-21T23:00:00Z", "until": "2026-11-22T00:00:00Z"}
    )
    assert selected["attempts"] == 1 and selected["measured_usd"] == "0"
    assert selected["unknown_attempts"] == 1 and selected["buckets"] == []
    assert ledger.report("project-a", group_by="month")["groups"]["2026-11"]["attempts"] == 2


@pytest.mark.parametrize(
    "filters",
    [
        {"since": "bad"},
        {"until": "2026-11-22"},
        {"since": "2026-11-22T00:00:00Z", "until": "2026-11-21T00:00:00Z"},
    ],
)
def test_invalid_usage_windows_do_not_turn_into_unfiltered_reports(setup, filters):
    with pytest.raises(LedgerError, match="report_time_invalid"):
        setup[0].report("project-a", filters=filters)


def test_risk_allowance_overrun_stops_project_until_explicit_review(setup):
    ledger, _ = setup
    attempt = admitted(setup)
    with ledger.store.transaction() as connection:
        connection.execute(
            "UPDATE attempts SET route_json=? WHERE attempt_id=?",
            ('{"provider_ref":"router","reservation_kind":"risk_allowance"}', attempt),
        )
    ledger.dispatch_intent("project-a", attempt)
    ledger.settle("project-a", attempt, status="measured", measured=Decimal("0.5"))
    with pytest.raises(LedgerError, match="admission_fenced"):
        admitted(setup, request("blocked"))
    review = dict(
        allowance=Decimal("0.6"),
        evidence_id="review-one",
        actor="operator",
        reason="Reviewed measured cost and raised allowance",
    )
    with pytest.raises(LedgerError, match="allowance_review_invalid"):
        ledger.acknowledge_allowance(
            "project-a", attempt, **{**review, "allowance": Decimal("0.4")}
        )
    assert ledger.acknowledge_allowance("project-a", attempt, **review)
    assert not ledger.acknowledge_allowance("project-a", attempt, **review)
    with pytest.raises(LedgerError, match="evidence_id_conflict"):
        ledger.acknowledge_allowance("project-a", attempt, **{**review, "reason": "changed"})
    admitted(setup, request("allowed"))
    assert ledger.report("project-a")["overrun"] is True
