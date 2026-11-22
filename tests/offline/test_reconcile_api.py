from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_config import private_file
from test_engine import ProviderDouble, configured
from test_ledger import request

from llm_gateway.service.api import create_app

pytestmark = pytest.mark.offline


def test_verified_operator_receipt_refreshes_retrieval_and_cache_without_inference(tmp_path):
    token = private_file(tmp_path, "owner.token", "a" * 48)

    def mutate(data):
        data["access"] = {
            "principals": {
                "owner": {
                    "token_file": str(token),
                    "aliases": ["summary"],
                    "operations": ["chat"],
                    "permissions": ["infer", "receipts:own", "admin:reconcile"],
                    "run_id": "run-one",
                }
            }
        }
        data["evidence_policy"] = {
            "retain_results": True,
            "cache_enabled": True,
            "result_ttl_seconds": 60,
        }

    provider = ProviderDouble(cost=None)
    engine, _, snapshot = configured(tmp_path, provider=provider, mutate=mutate)
    headers = {"Authorization": "Bearer " + "a" * 48}
    with TestClient(create_app(engine)) as client:
        first = client.post(
            "/v1/inferences", json=request().model_dump(mode="json"), headers=headers
        )
        assert (
            first.status_code == 200
            and first.json()["receipts"][0]["evidence_status"] == "estimated"
        )
        receipt = first.headers["X-Gateway-Receipt-Id"]
        evidence = {
            "evidence_id": "fixture-evidence",
            "request_id": "one",
            "generation_id": "gen-fixture",
            "measured_usd": "0.000003",
            "authority": "synthetic verified receipt",
            "observed_at": datetime.now(UTC).isoformat(),
            "verification": "operator_verified_receipt",
            "provider_ref": "router",
            "config_digest": snapshot.digest,
            "evidence_digest": "a" * 64,
        }
        assert (
            client.post(
                f"/v1/admin/receipts/{receipt}/reconcile",
                json=evidence | {"config_digest": "b" * 64},
                headers=headers,
            ).status_code
            == 409
        )
        response = client.post(
            f"/v1/admin/receipts/{receipt}/reconcile", json=evidence, headers=headers
        )
        assert response.status_code == 200 and response.json()["changed"]
        assert client.post(
            f"/v1/admin/receipts/{receipt}/reconcile", json=evidence, headers=headers
        ).json() == {"changed": False}
        updated = client.get("/v1/requests/one", headers=headers).json()
        assert updated["receipts"][0]["measured_usd"] == "0.000003"
        assert updated["receipts"][0]["estimated_usd"] is None
        reused = client.post(
            "/v1/inferences", json=request("two").model_dump(mode="json"), headers=headers
        )
        assert reused.json()["receipts"][0]["evidence_status"] == "cached_reuse"
        assert len(provider.calls) == 1
        assert engine.ledger.report("project-a")["measured_usd"] == "0.000003"
        conflict = client.post(
            f"/v1/admin/receipts/{receipt}/reconcile",
            json=evidence | {"evidence_id": "different", "measured_usd": "0.000004"},
            headers=headers,
        )
        assert conflict.status_code == 409


def test_unexpected_charge_on_zero_reserve_fences_only_that_project(tmp_path):
    from test_ledger import admitted

    from llm_gateway.storage.ledger import Ledger, LedgerError

    engine, authority, snapshot = configured(tmp_path, provider=ProviderDouble())
    ledger = Ledger(engine.store)
    # Internal zero-reserve test models the free-route anomaly, no provider runs.
    ledger.register("project-a", "owner", request(), snapshot)
    attempt = ledger.admit(
        "project-a",
        "owner",
        "one",
        ordinal=1,
        route={},
        reserve=Decimal("0"),
        buckets=[],
        max_attempts=1,
    )
    ledger.dispatch_intent("project-a", attempt)
    ledger.settle("project-a", attempt, status="measured", measured=Decimal("0.01"))
    with pytest.raises(LedgerError, match="admission_fenced"):
        admitted((ledger, snapshot), request("two"))
    with ledger.store.connection() as db:
        assert db.execute(
            "SELECT value FROM runtime_flags WHERE name='project_fenced:project-a'"
        ).fetchone()
        assert (
            db.execute("SELECT value FROM runtime_flags WHERE name='admission_fenced'").fetchone()
            is None
        )
