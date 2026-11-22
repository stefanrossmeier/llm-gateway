"""HTTP state/authority/capacity failures cannot authorize hidden inference."""

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from test_api import auth
from test_config import write_config
from test_ledger import request

from llm_gateway.config import ConfigSnapshot, load_project_config
from llm_gateway.config.loader import canonical_json
from llm_gateway.providers.runtime import ProviderFailure
from llm_gateway.service.api import create_app

pytestmark = pytest.mark.offline
pytest_plugins = ["test_api", "test_live_runner"]


def activate(engine, snapshot, tmp_path, mutate):
    data = snapshot.config.model_dump(mode="json")
    mutate(data)
    current = load_project_config(write_config(tmp_path, data), check_credentials=False)
    engine.registry.activate(current)
    return current


@pytest.mark.parametrize(
    "kind", ["fenced", "closing", "global_flag", "project_flag", "no_grants", "legacy"]
)
def test_unready_state_is_explicit_without_admitting_inference(service, tmp_path, kind):
    client, engine, _, snapshot = service
    if kind in {"fenced", "closing"}:
        setattr(engine, kind, True)
    elif kind in {"global_flag", "project_flag"}:
        name = "admission_fenced" if kind == "global_flag" else "project_fenced:project-a"
        with engine.store.transaction() as db:
            db.execute("INSERT INTO runtime_flags VALUES (?,?)", (name, "test"))
    elif kind == "no_grants":
        activate(
            engine,
            snapshot,
            tmp_path,
            lambda data: data["access"]["principals"]["owner"].update(permissions=[]),
        )
    else:
        legacy = "litellm-1.109.0/gateway-v3"
        data = snapshot.config.model_dump(mode="json")
        digest = hashlib.sha256(
            canonical_json(
                {
                    "project_config": data,
                    "fixtures": snapshot.fixtures,
                    "capability_revision": legacy,
                }
            ).encode()
        ).hexdigest()
        engine.registry.activate(
            ConfigSnapshot(
                snapshot.project_id,
                digest,
                canonical_json(data),
                snapshot.fixtures_payload,
                legacy,
            )
        )
    response = client.get("/ready", headers=auth())
    assert response.status_code == (403 if kind == "no_grants" else 503), response.text
    assert client.get("/health").json() == {"status": "alive"}
    assert engine.ledger.report("project-a")["attempts"] == 0


def test_live_readiness_requires_selected_credentials_before_dispatch(live_double, monkeypatch):
    plan, snapshot, client, engine, posts, _ = live_double
    monkeypatch.delenv("EXAMPLE_ROUTER_KEY")
    from llm_gateway.client import GatewayHTTPError

    with pytest.raises(GatewayHTTPError) as caught:
        client.ready()
    assert caught.value.status == 503
    assert not posts and not engine.provider.calls


async def test_reload_while_reading_body_cannot_reuse_stale_authentication(service, tmp_path):
    client, engine, _, snapshot = service
    raw = request().model_dump_json().encode()

    async def changing_body():
        yield raw[:1]
        activate(
            engine,
            snapshot,
            tmp_path,
            lambda data: data["aliases"]["summary"]["limits"].update(max_output_tokens=31),
        )
        yield raw[1:]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://testserver"
    ) as caller:
        response = await caller.post("/v1/inferences", content=changing_body(), headers=auth())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "configuration_changed_before_admission"
    with engine.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


@pytest.mark.parametrize("body", [b"{", b"[]", b'"text"', b"\xff"])
def test_nonobject_or_malformed_json_is_safe_before_registration(service, body):
    client, engine, _, _ = service
    response = client.post("/v1/inferences", content=body, headers=auth())
    assert response.status_code == 422
    assert engine.ledger.report("project-a")["attempts"] == 0


def test_invalid_body_identity_cannot_become_a_resource_path(service):
    client, engine, _, _ = service
    response = client.post(
        "/v1/inferences", json=request().model_dump(mode="json") | {"request_id": 7}, headers=auth()
    )
    assert response.status_code == 422 and response.json()["error"]["code"] == "idempotency_invalid"
    assert engine.ledger.report("project-a")["attempts"] == 0


def test_storage_failure_during_authentication_fences_and_redacts(service, monkeypatch):
    client, engine, _, _ = service

    def failure(*args, **kwargs):
        raise OSError("private file and secret exception canary")

    monkeypatch.setattr(engine.registry, "get", failure)
    response = client.get("/ready", headers=auth())
    assert response.status_code == 503 and engine.fenced
    assert response.json()["error"]["code"] == "ledger_unavailable"
    assert "canary" not in response.text


def test_missing_operator_reload_path_does_not_guess_another_file(service):
    _, engine, _, _ = service
    with TestClient(create_app(engine)) as client:
        response = client.post("/v1/admin/reload", json={}, headers=auth())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "reload_path_unavailable"


def test_unknown_reconciliation_receipt_is_not_an_upsert(service, tmp_path):
    client, engine, _, snapshot = service
    snapshot = activate(
        engine,
        snapshot,
        tmp_path,
        lambda data: data["access"]["principals"]["owner"]["permissions"].append("admin:reconcile"),
    )
    response = client.post(
        "/v1/admin/receipts/unknown/reconcile",
        headers=auth(),
        json={
            "evidence_id": "evidence",
            "request_id": "unknown",
            "generation_id": "generation",
            "measured_usd": "0.01",
            "authority": "synthetic offline evidence",
            "verification": "operator_verified_receipt",
            "provider_ref": "router",
            "config_digest": snapshot.digest,
            "evidence_digest": "0" * 64,
            "observed_at": datetime.now(UTC).isoformat(),
        },
    )
    assert response.status_code == 404 and response.json()["error"]["code"] == "receipt_unknown"
    assert engine.ledger.report("project-a")["attempts"] == 0


@pytest.mark.parametrize(
    "path,body",
    [
        (
            "/chat",
            {
                "workflow_id": "unknown",
                "session_id": "session",
                "turn": 1,
                "messages": [{"role": "user", "content": "public"}],
            },
        ),
        ("/v1/x-search", {"alias": "summary", "profile": "unknown", "purpose": "public"}),
        (
            "/v1/chat",
            {
                "alias": "summary",
                "profile": "unknown",
                "purpose": "public",
                "messages": [{"role": "user", "content": "public"}],
                "response_schema_name": "answer",
                "response_schema": {"type": "object"},
            },
        ),
    ],
)
def test_compatibility_profiles_cannot_select_ungranted_aliases(service, path, body):
    client, engine, _, _ = service
    response = client.post(path, json=body, headers=auth(identifier="profile"))
    assert (
        response.status_code == 403
        and response.json()["error"]["code"] == "profile_alias_forbidden"
    )
    assert engine.ledger.report("project-a")["attempts"] == 0


def test_pending_lookup_has_no_fabricated_attempt_receipt(service):
    client, engine, _, snapshot = service
    engine.ledger.register("project-a", "owner", request(), snapshot)
    response = client.get("/v1/requests/one", headers=auth())
    assert response.status_code == 202 and response.json()["receipts"] == []
    assert "X-Gateway-Receipt-Id" not in response.headers


def test_semantic_conflict_returns_the_original_owned_receipt_without_new_attempt(service):
    client, engine, _, _ = service
    first = client.post("/v1/inferences", json=request().model_dump(mode="json"), headers=auth())
    changed = request().model_dump(mode="json")
    changed["input"]["messages"][0]["content"] = "changed semantics"
    conflict = client.post("/v1/inferences", json=changed, headers=auth())
    assert conflict.status_code == 409
    assert conflict.headers["X-Gateway-Receipt-Id"] == first.headers["X-Gateway-Receipt-Id"]
    assert conflict.json()["receipts"] == first.json()["receipts"]
    assert conflict.json()["error"]["advice"] == "query_receipt"
    assert engine.ledger.report("project-a")["attempts"] == 1


@pytest.mark.parametrize(
    "code,status",
    [
        ("provider_auth", 503),
        ("provider_quota", 429),
        ("provider_deadline_exceeded", 502),
        ("provider_cancelled", 504),
    ],
)
def test_provider_failure_status_keeps_its_attempt_receipt(live_double, code, status):
    plan, snapshot, client, engine, posts, _ = live_double
    engine.provider.failure = ProviderFailure(code, dispatched=False)
    if code == "provider_cancelled":
        original = engine.provider.invoke

        def cancelled(*args, **kwargs):
            kwargs["cancel"].set()
            return original(*args, **kwargs)

        engine.provider.invoke = cancelled
    statuses = []
    client._http.event_hooks["response"].append(
        lambda response: statuses.append(response.status_code)
    )
    result = client.infer(plan.cases[0].request)
    # The lean client returns a typed result on inference failure HTTP statuses.
    assert result.error.code == code and len(result.receipts) == 1
    assert result.receipts[0].evidence_status == "not_dispatched"
    assert statuses == [status]
    assert len(posts) == 1 and len(engine.provider.calls) == 1
    with TestClient(create_app(engine)) as direct:
        response = direct.post(
            "/v1/inferences",
            json=plan.cases[0].request.model_dump(mode="json"),
            headers={"Authorization": "Bearer " + "t" * 48},
        )
    # Metadata-only replay reports unavailable content, preserving the original
    # attempt receipt rather than issuing another upstream call.
    assert response.status_code == 410
    assert response.json()["receipts"][0]["receipt_id"] == result.receipts[0].receipt_id
    assert len(engine.provider.calls) == 1


def test_submission_capacity_rejects_before_a_third_engine_call(service, monkeypatch):
    client, engine, _, _ = service
    engine.max_queue, engine.max_concurrency = 1, 1
    started, release = threading.Barrier(3), threading.Event()
    original = engine.run

    def blocked(*args, **kwargs):
        started.wait(timeout=10)
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "run", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [
            pool.submit(
                client.post,
                "/v1/inferences",
                json=request(name).model_dump(mode="json"),
                headers=auth(),
            )
            for name in ("first", "second")
        ]
        try:
            started.wait(timeout=10)
            denied = client.post(
                "/v1/inferences", json=request("third").model_dump(mode="json"), headers=auth()
            )
            assert denied.status_code == 429 and denied.json()["error"]["code"] == "queue_full"
            with engine.store.connection() as db:
                assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        finally:
            release.set()
        assert all(future.result(timeout=10).status_code == 200 for future in pending)
    assert engine.inflight == 0 and engine.total == 0


def test_disconnect_before_engine_registration_cancels_without_dispatch(service, monkeypatch):
    from starlette.requests import Request

    client, engine, _, _ = service
    original = engine.run

    async def disconnected(self):
        return True

    def waiting(*args, **kwargs):
        assert kwargs["cancel_signal"].wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    monkeypatch.setattr(engine, "run", waiting)
    response = client.post("/v1/inferences", json=request().model_dump(mode="json"), headers=auth())
    assert response.status_code == 504
    assert engine.ledger.report("project-a")["attempts"] == 0
    assert engine.inflight == 0 and engine.events == {}
    assert response.json()["receipts"] == []
