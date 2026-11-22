"""Retention and snapshot failures cannot alter financial history or fake progress."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from test_engine import configured
from test_ledger import request

from llm_gateway.config import ConfigError
from llm_gateway.contracts import RequestContext
from llm_gateway.storage import StoreError
from llm_gateway.storage.periods import period_window

pytestmark = pytest.mark.offline


def retained(tmp_path):
    return configured(
        tmp_path,
        mutate=lambda data: data.update(
            evidence_policy={"retain_results": True, "result_ttl_seconds": 60}
        ),
    )


def test_oversized_retained_result_preserves_original_payload_and_receipt(tmp_path):
    engine, authority, snapshot = retained(tmp_path)
    req = request()
    original = engine.run(authority, req)
    before = engine.ledger.report(authority.project)
    oversized = original.model_copy(update={"content": "x" * (16 * 1024 * 1024)})
    with pytest.raises(StoreError, match="result_retention_size_limit"):
        engine.results.retain(oversized, authority.principal, req, snapshot)
    assert engine.results.get(authority.project, authority.principal, req.request_id) == original
    assert engine.ledger.report(authority.project) == before
    assert len(engine.provider.calls) == 1


def test_retention_refuses_unowned_transaction_and_preserves_original_row(tmp_path):
    engine, authority, snapshot = retained(tmp_path)
    req = request()
    result = engine.run(authority, req)
    with engine.store.connection() as db:
        assert not db.in_transaction
        with pytest.raises(StoreError, match="result_transaction_required"):
            engine.results.retain(result, authority.principal, req, snapshot, connection=db)
    assert engine.results.get(authority.project, authority.principal, req.request_id) == result


def test_missing_fake_stage_does_not_consume_an_available_sequence(tmp_path):
    engine, authority, snapshot = configured(tmp_path, live=False)
    with pytest.raises(StoreError, match="fake_fixture_unavailable"):
        engine.results.next_fixture(
            snapshot, authority.principal, "summary", RequestContext(stage_id="missing")
        )
    assert (
        engine.results.next_fixture(snapshot, authority.principal, "summary", RequestContext())
        == "first"
    )
    assert engine.provider.calls == []


def test_same_digest_noncanonical_snapshot_bytes_cannot_replace_immutable_record(tmp_path):
    engine, _, snapshot = configured(tmp_path, live=False)
    equivalent = replace(snapshot, payload=json.dumps(json.loads(snapshot.payload), indent=2))
    assert equivalent.digest == snapshot.digest and equivalent.payload != snapshot.payload
    with pytest.raises(ConfigError, match="immutable_snapshot_conflict"):
        engine.registry.activate(equivalent)
    assert engine.registry.get(snapshot.project_id).payload == snapshot.payload


def test_unknown_budget_period_is_not_silently_reinterpreted():
    with pytest.raises(ValueError, match="period_invalid"):
        period_window("week", "UTC", datetime.now(UTC))
