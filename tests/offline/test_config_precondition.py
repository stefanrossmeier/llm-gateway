"""Reviewed configuration survives reload races without changing request identity."""

from dataclasses import replace

import httpx
import pytest
from test_api import auth
from test_config import write_config
from test_engine import configured
from test_ledger import request

from llm_gateway.client import AsyncGatewayClient, GatewayClient, GatewayError
from llm_gateway.config import load_project_config
from llm_gateway.contracts.reports import Readiness
from llm_gateway.service.engine import EngineError

pytestmark = pytest.mark.offline
pytest_plugins = ["test_api"]


def reload_changed(engine, snapshot, tmp_path):
    data = snapshot.config.model_dump(mode="json")
    data["aliases"]["summary"]["upstream_model"] = "changed/fixture"
    new = load_project_config(write_config(tmp_path, data), check_credentials=False)
    engine.registry.activate(new)
    return new


def test_reload_between_inspection_and_submission_never_registers_new_work(service, tmp_path):
    server, engine, _, snapshot = service
    ready = server.get("/ready", headers=auth()).json()
    assert ready["supports_config_precondition"] is True
    reload_changed(engine, snapshot, tmp_path)
    denied = server.post(
        "/v1/inferences",
        json=request().model_dump(mode="json"),
        headers=auth() | {"X-Gateway-Expected-Config-Digest": ready["config_digest"]},
    )
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "expected_configuration_conflict"
    assert engine.ledger.report("project-a")["attempts"] == 0
    with engine.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


@pytest.mark.parametrize("digest", ["", "A" * 64, "0" * 63, "0" * 65, "not-a-digest"])
def test_bad_header_is_rejected_without_admission(service, digest):
    server, engine, _, _ = service
    response = server.post(
        "/v1/inferences",
        json=request().model_dump(mode="json"),
        headers=auth() | {"X-Gateway-Expected-Config-Digest": digest},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "expected_configuration_digest_invalid"
    assert engine.ledger.report("project-a")["attempts"] == 0


@pytest.mark.parametrize(
    "path,payload",
    [
        (
            "/chat",
            {
                "workflow_id": "workflow",
                "session_id": "public-session",
                "turn": 1,
                "model_profile": "default",
                "messages": [{"role": "user", "content": "public"}],
            },
        ),
        (
            "/v1/chat/completions",
            {"model": "summary", "messages": [{"role": "user", "content": "public"}]},
        ),
    ],
)
def test_compatibility_surfaces_share_the_precondition(service, path, payload):
    server, engine, _, _ = service
    response = server.post(
        path,
        json=payload,
        headers=auth(identifier="compat") | {"X-Gateway-Expected-Config-Digest": "0" * 64},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "expected_configuration_conflict"
    assert engine.ledger.report("project-a")["attempts"] == 0


def test_replay_retains_original_snapshot_and_fingerprint_after_reload(service, tmp_path):
    server, engine, _, snapshot = service
    data = request().model_dump(mode="json")
    headers = auth() | {"X-Gateway-Expected-Config-Digest": snapshot.digest}
    first = server.post("/v1/inferences", json=data, headers=headers)
    assert first.status_code == 200
    reload_changed(engine, snapshot, tmp_path)
    assert server.post("/v1/inferences", json=data, headers=headers).json() == first.json()
    # A changed expected digest is a submission precondition, not request semantics.
    headers["X-Gateway-Expected-Config-Digest"] = "0" * 64
    assert server.post("/v1/inferences", json=data, headers=headers).json() == first.json()
    assert engine.ledger.report("project-a")["attempts"] == 1


def test_caller_precondition_cannot_replace_trusted_authentication_digest(tmp_path):
    engine, authority, snapshot = configured(tmp_path)
    new = reload_changed(engine, snapshot, tmp_path)
    stale = replace(authority, config_digest=snapshot.digest)
    with pytest.raises(EngineError, match="configuration_changed_before_admission"):
        engine.run(stale, request(), expected_config_digest=new.digest)
    assert not engine.provider.calls
    with engine.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


def test_sync_client_sends_header_and_validates_before_transport(service):
    server, _, token, snapshot = service
    posts = []

    def send(req):
        posts.append(req)
        return server.request(
            req.method, req.url.raw_path.decode(), content=req.content, headers=req.headers
        )

    with GatewayClient(
        "http://gateway.test", token_file=token, transport=httpx.MockTransport(send)
    ) as client:
        result = client.infer(request(), expected_config_digest=snapshot.digest)
        assert result.content == "first"
        assert posts[-1].headers["X-Gateway-Expected-Config-Digest"] == snapshot.digest
        for invalid in [1, "", "X" * 64]:
            with pytest.raises(GatewayError, match="expected_configuration_digest_invalid"):
                client.infer(request(), expected_config_digest=invalid)
    assert len(posts) == 1


async def test_async_client_sends_header_and_validates_before_transport(service):
    server, engine, _, snapshot = service
    async with AsyncGatewayClient(
        "http://gateway.test", token="a" * 48, transport=httpx.ASGITransport(app=server.app)
    ) as client:
        assert (await client.infer(request(), expected_config_digest=snapshot.digest)).content
        with pytest.raises(GatewayError, match="expected_configuration_digest_invalid"):
            await client.infer(request("two"), expected_config_digest=False)
    assert engine.ledger.report("project-a")["attempts"] == 1


def test_old_readiness_does_not_claim_support():
    ready = Readiness(status="ready", project_id="project-a", config_digest="0" * 64)
    assert ready.supports_config_precondition is False
