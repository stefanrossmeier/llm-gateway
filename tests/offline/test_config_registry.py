"""Snapshot isolation, durable storage and quota/timezone lineage are real integration tests."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from test_config import synthetic_config, write_config

from llm_gateway.config import ConfigError, load_project_config
from llm_gateway.config.models import BudgetPolicy
from llm_gateway.config.registry import ConfigRegistry
from llm_gateway.contracts import RequestContext
from llm_gateway.storage import StateStore, StoreError
from llm_gateway.storage.buckets import bind_bucket, ensure_bucket
from llm_gateway.storage.periods import period_window

pytestmark = pytest.mark.offline


def test_real_sqlite_durability_flags_and_rollback(tmp_path):
    store = StateStore(tmp_path / "state")
    with store.connection() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(RuntimeError), store.transaction() as db:
        db.execute("INSERT INTO runtime_flags VALUES ('must_rollback','yes')")
        raise RuntimeError("injected")
    with store.connection() as db:
        assert db.execute("SELECT * FROM runtime_flags").fetchall() == []


def test_database_and_root_permissions_and_future_schema(tmp_path, monkeypatch):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(StoreError, match="owner_only"):
        StateStore(public)
    monkeypatch.setattr("llm_gateway.storage.sqlite.filesystem_type", lambda _: "nfs")
    with pytest.raises(StoreError, match="local_filesystem"):
        StateStore(tmp_path / "remote")
    monkeypatch.setattr("llm_gateway.storage.sqlite.filesystem_type", lambda _: "apfs")
    store = StateStore(tmp_path / "future")
    with store.connection() as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(StoreError, match="schema_version_unsupported"):
        StateStore(tmp_path / "future")


def test_project_scoped_immutable_snapshots_survive_reload_and_restart(tmp_path):
    store = StateStore(tmp_path / "state")
    registry = ConfigRegistry(store)
    first = load_project_config(write_config(tmp_path, synthetic_config()))
    second = load_project_config(write_config(tmp_path, synthetic_config("project-b", "other")))
    registry.activate(first)
    registry.activate(second)
    changed = load_project_config(write_config(tmp_path, synthetic_config(model="changed")))
    registry.activate(changed)
    assert registry.get("project-a").digest == changed.digest
    assert (
        registry.get("project-a", first.digest).config.aliases["summary"].upstream_model
        == "fixture-model"
    )
    assert registry.get("project-b").config.aliases["summary"].upstream_model == "other"
    with pytest.raises(ConfigError, match="unknown"):
        registry.get("project-b", first.digest)
    with pytest.raises(ConfigError, match="conflict"):
        registry.activate(replace(first, payload=changed.payload))
    reopened = ConfigRegistry(StateStore(tmp_path / "state"))
    assert reopened.get("project-a").digest == changed.digest


def test_bad_reload_preserves_active_snapshot(tmp_path):
    registry = ConfigRegistry(StateStore(tmp_path / "state"))
    path = write_config(tmp_path, synthetic_config())
    original = registry.reload(path, "project-a")
    path.write_text("project_id: wrong")
    with pytest.raises(ConfigError):
        registry.reload(path, "project-a")
    assert registry.get("project-a").digest == original.digest


def test_budget_reload_cannot_reset_lineage_or_interrupt_inflight_calls(tmp_path):
    registry = ConfigRegistry(StateStore(tmp_path / "state"))
    data = synthetic_config()
    data["budgets"] = {"daily": {"scope": "project", "period": "day", "cap_usd": "1"}}
    original = load_project_config(write_config(tmp_path, data))
    registry.activate(original)
    with registry.store.transaction() as db:
        db.execute(
            "INSERT INTO requests(project_id,principal_id,request_id,fingerprint,config_digest,"
            "alias,operation,context_json,limits_json,trace_id,created_at,deadline_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "project-a",
                "owner",
                "one",
                "hash",
                original.digest,
                "summary",
                "chat",
                "{}",
                "{}",
                "trace",
                "now",
                "later",
            ),
        )
    data["budgets"]["daily"]["cap_usd"] = "2"
    changed = load_project_config(write_config(tmp_path, data))
    with pytest.raises(ConfigError, match="requires_drain"):
        registry.activate(changed)
    with registry.store.transaction() as db:
        db.execute("UPDATE requests SET execution_status='failed'")
        db.execute(
            "INSERT INTO budget_buckets VALUES ('bucket','project-a','daily','project-a',"
            "'period',NULL,NULL,'1','0.5')"
        )
    registry.activate(changed)
    data["budgets"]["renamed"] = data["budgets"].pop("daily")
    with pytest.raises(ConfigError, match="lineage"):
        registry.activate(load_project_config(write_config(tmp_path, data)))
    assert registry.get("project-a").digest == changed.digest
    with registry.store.connection() as db:
        assert db.execute("SELECT protected_usd FROM budget_buckets").fetchone()[0] == "0.5"


@pytest.mark.parametrize(
    "instant,hours",
    [
        (datetime(2026, 3, 29, 12, tzinfo=UTC), 23),
        (datetime(2026, 10, 25, 12, tzinfo=UTC), 25),
    ],
)
def test_berlin_daily_periods_preserve_dst_and_original_identity(instant, hours):
    period = period_window("day", "Europe/Berlin", instant)
    assert (period.end - period.start).total_seconds() == hours * 3600
    assert period_window("day", "Europe/Berlin", period.start).key == period.key


def test_month_boundary_and_naive_times():
    window = period_window("month", "UTC", datetime(2026, 12, 31, tzinfo=UTC))
    assert window.end == datetime(2027, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        period_window("day", "UTC", datetime(2026, 11, 11))


def test_budget_identity_is_stable_per_credential_and_does_not_reset_on_cap_change(tmp_path):
    store = StateStore(tmp_path / "state")
    context = RequestContext(run_id="run")
    kwargs = dict(
        project_id="project-a",
        principal_id="owner",
        request_id="one",
        provider_ref="router",
        alias="summary",
        context=context,
        timezone="UTC",
        now=datetime(2026, 11, 11, tzinfo=UTC),
    )
    policy = BudgetPolicy(scope="credential", period="day", cap_usd="1")
    first = bind_bucket("daily", policy, **kwargs)
    second = bind_bucket(
        "daily",
        BudgetPolicy(scope="credential", period="day", cap_usd="0.2"),
        **(kwargs | {"alias": "other", "request_id": "two"}),
    )
    assert first.bucket_id == second.bucket_id
    with store.transaction() as db:
        assert ensure_bucket(db, first) == 0
        db.execute("UPDATE budget_buckets SET protected_usd='0.3'")
        assert (
            ensure_bucket(db, second)
            == BudgetPolicy(scope="project", period="day", cap_usd="0.3").cap_usd
        )
        assert db.execute("SELECT cap_usd FROM budget_buckets").fetchone()[0] == "0.2"
    with store.connection() as db, pytest.raises(StoreError, match="transaction_required"):
        ensure_bucket(db, first)


def test_run_budget_scope_cannot_be_implicitly_invented():
    with pytest.raises(StoreError, match="unbound"):
        bind_bucket(
            "run",
            BudgetPolicy(scope="run", period="run", cap_usd="1"),
            project_id="project-a",
            principal_id="owner",
            request_id="one",
            provider_ref="router",
            alias="summary",
            context=RequestContext(),
            timezone="UTC",
            now=datetime(2026, 11, 11, tzinfo=UTC),
        )


def test_stale_reload_does_not_replace_a_newer_operator_revision(tmp_path):
    registry = ConfigRegistry(StateStore(tmp_path / "state"))
    first = load_project_config(write_config(tmp_path, synthetic_config()))
    registry.activate(first)
    second = load_project_config(write_config(tmp_path, synthetic_config(model="second")))
    registry.activate(second, expected_digest=first.digest, compare_active=True)
    third = load_project_config(write_config(tmp_path, synthetic_config(model="third")))
    with pytest.raises(ConfigError, match="reload_conflict"):
        registry.activate(third, expected_digest=first.digest, compare_active=True)
    assert registry.get("project-a").digest == second.digest


@pytest.mark.parametrize(
    "scope,period",
    [
        ("project", "day"),
        ("credential", "day"),
        ("run", "run"),
        ("workflow", "call"),
        ("alias", "month"),
    ],
)
def test_all_budget_scope_identities_are_project_bound(scope, period):
    kwargs = dict(
        principal_id="owner",
        request_id="call",
        provider_ref="router",
        alias="summary",
        context=RequestContext(run_id="run", workflow_id="workflow"),
        timezone="UTC",
        now=datetime(2026, 11, 11, tzinfo=UTC),
    )
    policy = BudgetPolicy(scope=scope, period=period, cap_usd="1")
    a = bind_bucket("budget", policy, project_id="a", **kwargs)
    b = bind_bucket("budget", policy, project_id="b", **kwargs)
    assert a.bucket_id != b.bucket_id
    assert a.cap_usd == policy.cap_usd
