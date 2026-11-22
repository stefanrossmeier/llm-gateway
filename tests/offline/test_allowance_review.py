"""Risk overruns require current configuration and explicit authorized review."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_config import private_file
from test_engine import configured
from test_ledger import bucket
from test_x_scope import mutate, search

from llm_gateway.service.api import create_app

pytestmark = pytest.mark.offline


def test_allowance_review_api_preserves_measured_cost_and_rejects_stale_scope(tmp_path):
    token = private_file(tmp_path, "operator.token", "a" * 48)
    reader = private_file(tmp_path, "reader.token", "r" * 48)

    def config(data):
        mutate(data)
        data["access"] = {
            "principals": {
                "operator": {"token_file": str(token), "permissions": ["admin:reconcile"]},
                "reader": {"token_file": str(reader), "permissions": ["receipts:own"]},
            }
        }

    engine, _, snapshot = configured(tmp_path, mutate=config)
    request = search()
    engine.ledger.register("project-a", "owner", request, snapshot)
    attempt = engine.ledger.admit(
        "project-a",
        "owner",
        request.request_id,
        ordinal=1,
        route={"provider_ref": "router", "reservation_kind": "risk_allowance"},
        reserve=Decimal("0.1"),
        buckets=[bucket(request)],
        max_attempts=1,
    )
    engine.ledger.dispatch_intent("project-a", attempt)
    engine.ledger.settle("project-a", attempt, status="measured", measured=Decimal("0.2"))
    body = {
        "config_digest": snapshot.digest,
        "evidence_id": "review",
        "reason": "Reviewed allowance",
    }
    headers = {"Authorization": "Bearer " + "a" * 48}
    route = f"/v1/admin/receipts/{attempt}/acknowledge-allowance"
    with TestClient(create_app(engine)) as client:
        assert client.post(route, json=body).status_code == 401
        assert (
            client.post(
                route, json=body, headers={"Authorization": "Bearer " + "r" * 48}
            ).status_code
            == 403
        )
        assert (
            client.post(
                route, json={**body, "config_digest": "0" * 64}, headers=headers
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/v1/admin/receipts/missing/acknowledge-allowance", json=body, headers=headers
            ).status_code
            == 404
        )
        assert client.post(route, json=body, headers=headers).json() == {"changed": True}
        assert client.post(route, json=body, headers=headers).json() == {"changed": False}
    assert engine.ledger.report("project-a")["measured_usd"] == "0.2"
