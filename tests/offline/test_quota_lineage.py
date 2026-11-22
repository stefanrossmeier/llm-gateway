"""A configuration reload cannot buy another daily X search under a fresh label."""

import copy
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from test_config import write_config
from test_ledger import request
from test_paid_proposal import project
from test_x_scope import payload, search, x_engine

from llm_gateway.config import ConfigError, load_project_config
from llm_gateway.config.registry import ConfigRegistry
from llm_gateway.service.engine import EngineError
from llm_gateway.storage import StateStore
from llm_gateway.storage.ledger import Ledger

pytestmark = pytest.mark.offline


@pytest.mark.parametrize("change", ["rename", "add", "rewire"])
def test_consumed_search_policy_cannot_be_reset_by_reload(tmp_path, change, monkeypatch):
    req = search()
    engine, authority, snapshot = x_engine(tmp_path, payload(req), monkeypatch)
    if change == "rewire":
        initial = snapshot.config.model_dump(mode="json")
        initial["operation_policies"]["other-existing"] = copy.deepcopy(
            initial["operation_policies"]["search"]
        )
        snapshot = load_project_config(write_config(tmp_path, initial), check_credentials=False)
        engine.registry.activate(snapshot)
    result = engine.run(authority, req)
    assert result.output_status == "valid" and len(engine.provider.calls) == 1
    data = snapshot.config.model_dump(mode="json")
    if change == "rename":
        data["operation_policies"]["renamed"] = data["operation_policies"].pop("search")
        data["aliases"]["summary"]["operation_policy_ref"] = "renamed"
    elif change == "add":
        data["operation_policies"]["new-allowance"] = copy.deepcopy(
            data["operation_policies"]["search"]
        )
    else:
        data["aliases"]["summary"]["operation_policy_ref"] = "other-existing"
    with pytest.raises(ConfigError, match="quota_lineage_migration_required"):
        engine.registry.activate(
            load_project_config(write_config(tmp_path, data), check_credentials=False)
        )
    assert engine.registry.get("project-a").digest == snapshot.digest
    # A different prompt must not become a cache hit or create another paid dispatch.
    second = search("two")
    second = second.model_copy(
        update={"input": second.input.model_copy(update={"input": "different public prompt"})}
    )
    with pytest.raises(EngineError, match="quota_consumed"):
        engine.run(authority, second)
    assert len(engine.provider.calls) == 1
    assert result.receipts[0].measured_usd == Decimal("0.001")


def test_unused_policy_rename_is_allowed_only_after_inflight_work_drains(tmp_path):
    snapshot = project(tmp_path)
    registry = ConfigRegistry(StateStore(tmp_path / "state"))
    registry.activate(snapshot)
    ledger = Ledger(registry.store)
    req = request("inflight")
    ledger.register(snapshot.project_id, "test", req, snapshot)
    data = snapshot.config.model_dump(mode="json")
    data["operation_policies"]["renamed"] = data["operation_policies"].pop("scope")
    data["aliases"]["search"]["operation_policy_ref"] = "renamed"
    changed = load_project_config(write_config(tmp_path, data), check_credentials=False)
    with pytest.raises(ConfigError, match="quota_reload_requires_drain"):
        registry.activate(changed)
    ledger.finish(
        snapshot.project_id,
        "test",
        req.request_id,
        execution_status="cancelled",
        output_status="unavailable",
    )
    registry.activate(changed)
    assert registry.get(snapshot.project_id).digest == changed.digest


def test_policy_criteria_change_keeps_the_consumed_identity_and_cannot_redispatch(
    tmp_path, monkeypatch
):
    req = search()
    engine, authority, snapshot = x_engine(tmp_path, payload(req), monkeypatch)
    engine.run(authority, req)
    data = snapshot.config.model_dump(mode="json")
    data["operation_policies"]["search"]["max_posts"] = 5
    changed = load_project_config(write_config(tmp_path, data), check_credentials=False)
    engine.registry.activate(changed)
    assert engine.registry.get("project-a").digest == changed.digest
    with pytest.raises(EngineError, match="quota_consumed"):
        engine.run(authority, search("two"))
    assert len(engine.provider.calls) == 1


def test_recorded_fake_history_cannot_be_promoted_to_live_in_place(tmp_path):
    snapshot = project(tmp_path)
    data = snapshot.config.model_dump(mode="json")
    data["mode"] = "fake"
    original = load_project_config(write_config(tmp_path, data), check_credentials=False)
    registry = ConfigRegistry(StateStore(tmp_path / "state"))
    registry.activate(original)
    Ledger(registry.store).register(original.project_id, "test", request(), original)
    data["mode"] = "live"
    with pytest.raises(ConfigError, match="mode_change_requires_new_project_namespace"):
        registry.activate(
            load_project_config(write_config(tmp_path, data), check_credentials=False)
        )
    assert registry.get(original.project_id).digest == original.digest


def test_quota_timezone_is_stable_even_without_monetary_buckets(tmp_path):
    snapshot = project(tmp_path)
    data = snapshot.config.model_dump(mode="json")
    data["mode"], data["budgets"] = "fake", {}
    for alias in data["aliases"].values():
        alias["budget_policy_refs"] = []
    original = load_project_config(write_config(tmp_path, data), check_credentials=False)
    registry = ConfigRegistry(StateStore(tmp_path / "state"))
    registry.activate(original)
    ledger = Ledger(registry.store)
    req = request()
    ledger.register(original.project_id, "test", req, original)
    attempt = ledger.admit(
        original.project_id,
        "test",
        req.request_id,
        ordinal=1,
        route={},
        reserve=Decimal("0"),
        buckets=[],
        max_attempts=1,
    )
    ledger.dispatch_intent(
        original.project_id, attempt, quota=("scope", datetime.now(UTC).date().isoformat())
    )
    ledger.settle(original.project_id, attempt, status="simulated")
    ledger.finish(
        original.project_id,
        "test",
        req.request_id,
        execution_status="completed",
        output_status="valid",
    )
    data["timezone"] = "Europe/Berlin"
    with pytest.raises(ConfigError, match="timezone_lineage_migration_required"):
        registry.activate(
            load_project_config(write_config(tmp_path, data), check_credentials=False)
        )
    assert registry.get(original.project_id).digest == original.digest
