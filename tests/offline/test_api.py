from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_config import private_file, synthetic_config, write_config
from test_engine import configured
from test_ledger import request as canonical_request

from llm_gateway.config import load_project_config
from llm_gateway.service.api import create_app
from llm_gateway.service.engine import Authority

pytestmark = pytest.mark.offline


@pytest.fixture
def service(tmp_path):
    token_path = private_file(tmp_path, "owner.token", "a" * 48)
    readonly = private_file(tmp_path, "reader.token", "r" * 48)

    def mutate(data):
        data["aliases"]["summary"]["limits"].update(
            attempt_timeout_seconds=5, request_timeout_seconds=10
        )
        data["access"] = {
            "principals": {
                "owner": {
                    "token_file": str(token_path),
                    "aliases": ["summary"],
                    "operations": ["chat"],
                    "permissions": ["infer", "receipts:own", "usage:project", "admin:reload"],
                    "run_id": "run-one",
                },
                "reader": {"token_file": str(readonly), "permissions": ["receipts:own"]},
            }
        }
        data["evidence_policy"] = {"retain_results": True, "result_ttl_seconds": 60}
        data["compatibility"] = {
            "safeplane_profiles": {"workflow": {"default": "summary"}},
            "dashboard_profiles": {"dashboard": ["summary"]},
        }

    engine, authority, snapshot = configured(tmp_path, live=False, mutate=mutate)
    app = create_app(engine, project_configs={"project-a": tmp_path / "project.yaml"})
    with TestClient(app) as client:
        yield client, engine, token_path, snapshot


def auth(token="a" * 48, identifier=None):
    return {
        "Authorization": "Bearer " + token,
        **({"Idempotency-Key": identifier} if identifier else {}),
    }


def test_no_implicit_loopback_auth_and_health_never_infers(service):
    client, engine, _, snapshot = service
    assert client.get("/health").json() == {"status": "alive"}
    assert client.get("/ready").status_code == 401
    assert client.get("/ready", headers=auth()).json()["config_digest"] == snapshot.digest
    assert client.get("/ready", headers=auth()).json()["principal_id"] == "owner"
    assert client.get("/ready", headers=auth("r" * 48)).json()["principal_id"] == "reader"
    response = client.get("/v1/aliases", headers=auth())
    assert response.status_code == 200 and [a["alias"] for a in response.json()["aliases"]] == [
        "summary"
    ]
    assert engine.ledger.report("project-a")["attempts"] == 0


def test_caller_cannot_supply_internal_verified_ipc_authority(service):
    client, engine, _, _ = service
    data = canonical_request().model_dump(mode="json") | {"owner_ipc": True}
    assert client.post("/v1/inferences", json=data, headers=auth()).status_code == 422
    assert engine.ledger.report("project-a")["attempts"] == 0


@pytest.mark.parametrize(
    "field",
    [
        "custom_tokenizer",
        "tokenizer",
        "trainer",
        "callbacks",
        "custom_llm_provider",
        "api_base",
    ],
)
def test_custom_sdk_or_training_controls_cannot_reach_the_provider(service, field):
    client, engine, _, _ = service
    for location in ["request", "input"]:
        payload = canonical_request().model_dump(mode="json")
        target = payload if location == "request" else payload["input"]
        target[field] = {"unsafe": "synthetic-private-control-canary"}
        result = client.post("/v1/inferences", json=payload, headers=auth())
        assert result.status_code == 422
        assert "synthetic-private-control-canary" not in result.text
    assert engine.ledger.report("project-a")["attempts"] == 0


def test_training_operation_cannot_be_selected_as_gateway_inference(service):
    client, engine, _, _ = service
    payload = canonical_request().model_dump(mode="json") | {"operation": "train"}
    assert client.post("/v1/inferences", json=payload, headers=auth()).status_code == 422
    assert engine.ledger.report("project-a")["attempts"] == 0


def test_own_or_run_filtered_usage_omits_shared_global_bucket_balances(tmp_path):
    tokens = {
        name: private_file(tmp_path, name + ".token", char * 48)
        for name, char in [("first", "a"), ("second", "b"), ("auditor", "c")]
    }

    def mutate(data):
        data["budgets"]["shared-day"] = {"scope": "project", "period": "day", "cap_usd": "1"}
        data["aliases"]["summary"]["budget_policy_refs"].append("shared-day")
        data["access"] = {
            "principals": {
                name: {
                    "token_file": str(tokens[name]),
                    "aliases": ["summary"],
                    "operations": ["chat"],
                    "permissions": ["usage:project"]
                    if name == "auditor"
                    else ["infer", "receipts:own"],
                    **({"run_id": name + "-run"} if name != "auditor" else {}),
                }
                for name in tokens
            }
        }

    engine, _, _ = configured(tmp_path, mutate=mutate)
    with TestClient(create_app(engine)) as client:
        for name, char in [("first", "a"), ("second", "b")]:
            data = canonical_request(name, context={"run_id": name + "-run"})
            response = client.post(
                "/v1/inferences", json=data.model_dump(mode="json"), headers=auth(char * 48)
            )
            assert response.status_code == 200
        own = client.get("/v1/usage", headers=auth()).json()
        assert own["attempts"] == 1 and own["measured_usd"] == "0.0001"
        assert own["buckets"] == []
        scoped = client.get("/v1/usage?run_id=first-run", headers=auth("c" * 48)).json()
        assert scoped["attempts"] == 1 and scoped["buckets"] == []
        project = client.get("/v1/usage", headers=auth("c" * 48)).json()
        assert project["attempts"] == 2 and project["measured_usd"] == "0.0002"
        assert any(bucket["policy_id"] == "shared-day" for bucket in project["buckets"])


def test_idempotency_headers_receipts_and_private_result_expiry(service):
    client, engine, _, _ = service
    data = canonical_request().model_dump(mode="json")
    assert client.post("/v1/inferences", json=data).status_code == 401
    missing = {k: v for k, v in data.items() if k != "request_id"}
    assert client.post("/v1/inferences", json=missing, headers=auth()).status_code == 428
    first = client.post("/v1/inferences", json=missing, headers=auth(identifier="one"))
    assert first.status_code == 200 and first.json()["content"] == "first"
    receipt_id = first.headers["X-Gateway-Receipt-Id"]
    assert client.post("/v1/inferences", json=data, headers=auth()).json() == first.json()
    assert (
        client.get("/v1/receipts/" + receipt_id, headers=auth()).json()["evidence_status"]
        == "simulated"
    )
    assert client.get("/v1/receipts/" + receipt_id, headers=auth("r" * 48)).status_code == 404
    assert (
        client.post("/v1/inferences", json=data, headers=auth(identifier="other")).status_code
        == 409
    )
    with engine.store.transaction() as db:
        db.execute(
            "UPDATE retained_results SET expires_at=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )
    replay = client.post("/v1/inferences", json=data, headers=auth())
    assert replay.status_code == 410 and replay.json()["receipts"]
    assert engine.ledger.report("project-a")["attempts"] == 1


def test_token_rotation_keeps_principal_and_receipt_ownership(service):
    client, _, path, snapshot = service
    first = client.post(
        "/v1/inferences", json=canonical_request().model_dump(mode="json"), headers=auth()
    )
    path.write_text("n" * 48)
    assert client.get("/ready", headers=auth()).status_code == 401
    assert client.get("/ready", headers=auth("n" * 48)).json()["config_digest"] == snapshot.digest
    assert (
        client.get(
            "/v1/receipts/" + first.headers["X-Gateway-Receipt-Id"], headers=auth("n" * 48)
        ).status_code
        == 200
    )


def test_unknown_fields_and_secret_input_are_not_echoed(service):
    client, engine, _, _ = service
    data = canonical_request().model_dump(mode="json") | {"api_key": "must-not-echo"}
    response = client.post("/v1/inferences", json=data, headers=auth())
    assert response.status_code == 422 and "must-not-echo" not in response.text
    assert engine.ledger.report("project-a")["attempts"] == 0
    response = client.post("/v1/inferences", content="{broken", headers=auth())
    assert response.status_code == 422
    assert client.get("/v1/aliases", headers=auth("r" * 48)).status_code == 403
    assert (
        client.post("/v1/admin/receipts/unknown/reconcile", json={}, headers=auth()).status_code
        == 403
    )


def test_usage_scope_and_readonly_permissions(service):
    client, _, _, _ = service
    client.post("/v1/inferences", json=canonical_request().model_dump(mode="json"), headers=auth())
    report = client.get("/v1/usage?group_by=alias", headers=auth())
    assert report.status_code == 200 and report.json()["attempts"] == 1
    assert client.get("/v1/usage?run_id=spoof", headers=auth()).status_code == 403
    assert client.get("/v1/usage", headers=auth("r" * 48)).json()["attempts"] == 0
    assert (
        client.post(
            "/v1/inferences",
            json=canonical_request("two").model_dump(mode="json"),
            headers=auth("r" * 48),
        ).status_code
        == 403
    )


def test_safeplane_adapter_uses_owner_mapping_and_unique_invocation(service):
    client, engine, _, _ = service
    payload = {
        "workflow_id": "workflow",
        "model_profile": "default",
        "session_id": "session",
        "turn": 1,
        "messages": [{"role": "user", "content": "public"}],
        "timeout_seconds": 5,
    }
    response = client.post("/chat", json=payload, headers=auth(identifier="safeplane-one"))
    assert response.status_code == 200
    assert response.json()["message"]["content"] == "first"
    assert response.json()["model"]["configured_model"] == "fixture-model"
    assert (
        client.post(
            "/chat", json=payload | {"workflow_id": "other"}, headers=auth(identifier="bad-profile")
        ).status_code
        == 403
    )
    assert engine.ledger.report("project-a")["attempts"] == 1


def test_actual_openai_sdk_chat_uses_alias_and_receipt_headers(service):
    from openai import OpenAI

    client, engine, _, _ = service
    sdk = OpenAI(
        api_key="a" * 48, base_url="http://testserver/v1", http_client=client, max_retries=0
    )
    raw = sdk.chat.completions.with_raw_response.create(
        model="summary",
        messages=[{"role": "user", "content": "public"}],
        extra_headers={"Idempotency-Key": "sdk-chat"},
    )
    assert raw.parse().choices[0].message.content == "first"
    assert raw.headers["X-Gateway-Receipt-Id"]
    assert raw.parse().usage is None
    assert (
        client.post(
            "/v1/chat/completions",
            json={"model": "summary", "messages": [], "stream": True},
            headers=auth(identifier="stream"),
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/responses",
            json={"model": "summary", "input": "public", "previous_response_id": "untrusted"},
            headers=auth(identifier="state"),
        ).status_code
        == 422
    )
    assert engine.ledger.report("project-a")["attempts"] == 1


def test_cross_project_config_readiness_aliases_budget_and_receipts_are_isolated(service, tmp_path):
    client, engine, _, first_snapshot = service
    token = private_file(tmp_path, "b.token", "b" * 48)
    fixtures = tmp_path / "b-fixtures"
    fixtures.mkdir()
    (fixtures / "result.txt").write_text("project-b-result")
    data = synthetic_config("project-b", "other-model")
    data["fake"] = {
        "fixture_root": str(fixtures),
        "responses": {"summary": {"default": ["result.txt"]}},
    }
    data["access"] = {
        "principals": {
            "owner": {
                "token_file": str(token),
                "aliases": ["summary"],
                "operations": ["chat"],
                "permissions": ["infer", "receipts:own", "usage:project"],
            }
        }
    }
    snapshot = load_project_config(write_config(tmp_path, data))
    engine.registry.activate(snapshot)
    a = client.post(
        "/v1/inferences", json=canonical_request().model_dump(mode="json"), headers=auth()
    )
    b = client.post(
        "/v1/inferences", json=canonical_request().model_dump(mode="json"), headers=auth("b" * 48)
    )
    assert a.json()["content"] == "first" and b.json()["content"] == "project-b-result"
    assert b.json()["model"]["upstream_model"] == "other-model"
    assert client.get("/ready", headers=auth()).json()["config_digest"] == first_snapshot.digest
    assert client.get("/ready", headers=auth("b" * 48)).json()["config_digest"] == snapshot.digest
    assert (
        client.get(
            "/v1/receipts/" + a.headers["X-Gateway-Receipt-Id"], headers=auth("b" * 48)
        ).status_code
        == 404
    )
    assert client.get("/v1/usage", headers=auth("b" * 48)).json()["attempts"] == 1


def test_operator_reload_keeps_old_receipt_snapshot_and_rejects_request_paths(service, tmp_path):
    import yaml

    client, engine, _, snapshot = service
    data = canonical_request().model_dump(mode="json")
    first = client.post("/v1/inferences", json=data, headers=auth())
    assert (
        client.post(
            "/v1/admin/reload", json={"path": "https://evil.invalid"}, headers=auth()
        ).status_code
        == 422
    )
    path = tmp_path / "project.yaml"
    changed = yaml.safe_load(path.read_text())
    changed["aliases"]["summary"]["upstream_model"] = "new-fixture-model"
    path.write_text(yaml.safe_dump(changed))
    response = client.post("/v1/admin/reload", json={}, headers=auth())
    assert response.status_code == 200 and response.json()["config_digest"] != snapshot.digest
    replay = client.post("/v1/inferences", json=data, headers=auth())
    assert replay.json()["model"]["configured_model"] == "fixture-model"
    assert replay.json()["config_digest"] == first.json()["config_digest"]
    assert engine.ledger.report("project-a")["attempts"] == 1


def test_result_recovery_projection_and_unknown_format(service):
    client, engine, _, _ = service
    engine.run(
        Authority(
            "project-a", "owner", engine.registry.get("project-a").config.access.principals["owner"]
        ),
        canonical_request("recover"),
    )
    projected = client.get("/v1/requests/recover?format=dashboard", headers=auth())
    assert projected.status_code == 200 and projected.json()["content"] == "first"
    assert client.get("/v1/requests/recover?format=other", headers=auth()).status_code == 422
    assert (
        client.get("/v1/requests/recover?format=dashboard", headers=auth("r" * 48)).status_code
        != 200
    )
