"""T081: frozen synthetic wire contracts; no consumer checkout/runtime dependency."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_config import private_file
from test_engine import configured

from llm_gateway.service.api import create_app

pytestmark = pytest.mark.offline
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "consumers"


def service(tmp_path, fixture):
    token = private_file(tmp_path, "contract.token", "c" * 48)

    def mutate(data):
        data["project_id"] = "contract-" + fixture["consumer"]
        profile = data["aliases"]["summary"]
        profile["operation"] = fixture["operation"]
        profile["capabilities"] = ["plain_text", "json_schema", "function_calls"]
        if fixture["operation"] == "responses":
            profile["capabilities"] += ["reasoning_effort", "text_verbosity"]
        profile["limits"].update(
            max_input_bytes=8192,
            max_input_tokens=16384,
            attempt_timeout_seconds=5,
            request_timeout_seconds=10,
        )
        data["access"] = {
            "principals": {
                "contract-reader": {
                    "token_file": str(token),
                    "aliases": ["summary"],
                    "operations": [fixture["operation"]],
                    "permissions": ["infer", "receipts:own"],
                    "run_id": "run-one",
                }
            }
        }
        data["compatibility"] = {
            "safeplane_profiles": {"example-workflow": {"example-profile": "summary"}},
            "dashboard_profiles": {"example-profile": ["summary"]},
        }
        data["fake"]["responses"]["summary"] = {
            "default": ["first.txt", "second.txt"],
            "route": ["first.txt", "second.txt"],
            "review-one": ["first.txt", "second.txt"],
        }
        for name, payload in [
            ("first.txt", fixture["payload"]),
            ("second.txt", fixture.get("second_payload", fixture["payload"])),
        ]:
            (tmp_path / "fixtures" / name).write_text(
                payload if isinstance(payload, str) else json.dumps(payload)
            )

    engine, _, snapshot = configured(tmp_path, live=False, mutate=mutate)
    return TestClient(create_app(engine)), engine, snapshot


@pytest.mark.parametrize("name", [p.stem for p in sorted(FIXTURES.glob("*.json"))])
def test_frozen_consumer_wire_shape_and_receipt_tombstone(tmp_path, name):
    fixture = json.loads((FIXTURES / (name + ".json")).read_text())
    assert fixture["synthetic"] is True and fixture["contract_revision"] == "v1"
    client, engine, snapshot = service(tmp_path, fixture)
    headers = {"Authorization": "Bearer " + "c" * 48, "Idempotency-Key": "contract:one"}
    with client:
        response = client.post(fixture["route"], json=fixture["request"], headers=headers)
        assert response.status_code == 200, response.json()
        body = response.json()
        if name == "workflow-chat":
            assert body["message"]["content"] == fixture["payload"]
            assert body["model"]["workflow_id"] == "example-workflow"
            second = client.post(
                fixture["route"],
                json=fixture["request"] | {"turn": 2},
                headers=headers | {"Idempotency-Key": "contract:two"},
            )
            assert second.json()["message"]["content"] == fixture["second_payload"]
        elif name in {"dashboard-a", "dashboard-b"}:
            assert body["content"] == fixture["payload"]
            assert body["usage"]["cost_usd"] is None and body["provider"] is None
            assert body["provider_metadata"]["receipts"][0]["evidence_status"] == "simulated"
        elif name == "canonical-chat":
            assert body["content"] == fixture["payload"]
            assert body["receipts"][0]["measured_usd"] is None
            assert body["receipts"][0]["evidence_status"] == "simulated"
        else:
            assert body["store"] is False and body["parallel_tool_calls"] is False
            assert body["output"][0]["call_id"] == "call-public"
            assert body["output"][0]["arguments"] == '{"ok":true}'
            assert body["usage"] is None
        row = engine.ledger.request(snapshot.project_id, "contract-reader", "contract:one")
        context = json.loads(row["context_json"])
        assert all(context[key] == value for key, value in fixture["expected_context"].items())
        receipt_id = response.headers["X-Gateway-Receipt-Id"]
        receipt = client.get("/v1/receipts/" + receipt_id, headers=headers).json()
        assert receipt["project_id"] == snapshot.project_id
        assert receipt["request_id"] == "contract:one" and receipt["measured_usd"] is None
        assert receipt["config_digest"] == snapshot.digest
        # Replays retain accounting and do not silently consume another fixture.
        replay = client.post(fixture["route"], json=fixture["request"], headers=headers)
        assert replay.status_code == 410
        assert replay.json()["receipts"][0] == receipt


@pytest.mark.parametrize("name", [p.stem for p in sorted(FIXTURES.glob("*.json"))])
def test_consumer_surface_cannot_supply_credentials_destination_or_authority(tmp_path, name):
    fixture = json.loads((FIXTURES / (name + ".json")).read_text())
    client, engine, snapshot = service(tmp_path, fixture)
    headers = {"Authorization": "Bearer " + "c" * 48, "Idempotency-Key": "contract:rejected"}
    with client:
        for field, value in [
            ("api_key", "synthetic-private-input-canary"),
            ("base_url", "https://untrusted.invalid"),
            ("owner_ipc", True),
            ("project_id", "different-project"),
        ]:
            response = client.post(
                fixture["route"], json=fixture["request"] | {field: value}, headers=headers
            )
            assert response.status_code == 422
            assert "synthetic-private-input-canary" not in response.text
        assert engine.ledger.report(snapshot.project_id)["attempts"] == 0
