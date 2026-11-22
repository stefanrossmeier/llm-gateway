import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient
from test_config import private_file
from test_engine import configured

from llm_gateway.service.api import create_app

pytestmark = pytest.mark.offline


def operation_service(tmp_path, operation, payload, *, max_body_bytes=None):
    token = private_file(tmp_path, "token", "a" * 48)

    def mutate(data):
        profile = data["aliases"]["summary"]
        profile["limits"].update(attempt_timeout_seconds=5, request_timeout_seconds=10)
        profile.update(
            operation=operation,
            capabilities={
                "chat": ["plain_text", "json_schema"],
                "responses": ["plain_text", "function_calls", "json_object", "json_schema"],
                "embeddings": ["embedding_dimensions"],
                "decisions": [],
            }[operation],
        )
        if operation == "decisions":
            profile["limits"]["max_output_tokens"] = None
        if operation == "embeddings":
            profile["limits"].update(max_output_tokens=None, embedding_dimensions=2)
        data["access"] = {
            "principals": {
                "owner": {
                    "token_file": str(token),
                    "aliases": ["summary"],
                    "operations": [operation],
                    "permissions": ["infer", "receipts:own"],
                    "run_id": "run-one",
                }
            }
        }
        data["compatibility"] = {"dashboard_profiles": {"dashboard": ["summary"]}}
        (tmp_path / "fixtures" / "first.txt").write_text(json.dumps(payload))

    engine, _, _ = configured(tmp_path, live=False, mutate=mutate)
    options = {"max_body_bytes": max_body_bytes} if max_body_bytes is not None else {}
    return TestClient(create_app(engine, **options)), engine


def headers(identifier):
    return {"Authorization": "Bearer " + "a" * 48, "Idempotency-Key": identifier}


def test_actual_sdk_responses_keeps_item_order_and_stateless_function_calls(tmp_path):
    from openai import OpenAI

    payload = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg-before",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "before", "annotations": []}],
            },
            {
                "type": "function_call",
                "id": "fc-fixture",
                "call_id": "call-fixture",
                "name": "read",
                "arguments": "{}",
                "status": "completed",
            },
            {
                "type": "message",
                "id": "msg-after",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "after", "annotations": []}],
            },
        ],
    }
    client, engine = operation_service(tmp_path, "responses", payload)
    with client:
        sdk = OpenAI(
            api_key="a" * 48, base_url="http://testserver/v1", http_client=client, max_retries=0
        )
        response = sdk.responses.create(
            model="summary",
            input="public",
            tools=[{"type": "function", "name": "read", "parameters": {"type": "object"}}],
            tool_choice={"type": "function", "name": "read"},
            store=False,
            parallel_tool_calls=False,
            extra_headers={"Idempotency-Key": "responses"},
        )
        assert [item.type for item in response.output] == ["message", "function_call", "message"]
        assert response.output[1].arguments == "{}" and response.output[1].call_id == "call-fixture"
        assert response.usage is None
        denied = client.post(
            "/v1/responses",
            json={"model": "summary", "input": "public", "store": True},
            headers=headers("state"),
        )
        assert denied.status_code == 422
        assert engine.ledger.report("project-a")["attempts"] == 1


def test_embeddings_compatible_shape_is_ordered_and_missing_usage_is_null(tmp_path):
    payload = {
        "data": [{"index": 1, "embedding": [0.2, 0.3]}, {"index": 0, "embedding": [0.1, 0.2]}]
    }
    client, _ = operation_service(tmp_path, "embeddings", payload)
    with client:
        response = client.post(
            "/v1/embeddings",
            json={"model": "summary", "input": ["a", "b"], "dimensions": 2},
            headers=headers("embedding"),
        )
        assert response.status_code == 200
        assert [row["index"] for row in response.json()["data"]] == [0, 1]
        assert response.json()["usage"] is None and response.headers["X-Gateway-Receipt-Id"]


def test_dashboard_schema_and_nullable_cost_contract(tmp_path):
    client, _ = operation_service(tmp_path, "chat", {"ok": True})
    with client:
        result = client.post(
            "/v1/chat",
            json={
                "alias": "summary",
                "purpose": "ranking",
                "profile": "dashboard",
                "messages": [{"role": "user", "content": "public"}],
                "response_schema_name": "answer",
                "response_schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            },
            headers=headers("dashboard"),
        )
        assert result.status_code == 200 and result.json()["content"] == {"ok": True}
        assert result.json()["usage"]["cost_usd"] is None
        assert result.json()["provider_metadata"]["receipts"][0]["evidence_status"] == "simulated"


def test_decisions_compatible_alias_preserves_probabilities(tmp_path):
    client, _ = operation_service(
        tmp_path, "decisions", {"answers": {"ok": {"type": "noul", "noul": 0.9}}}
    )
    with client:
        response = client.post(
            "/decisions",
            json={
                "model": "summary",
                "state": {"x": "public"},
                "questions": {"ok": {"type": "noul", "instructions": "ok?"}},
                "timeout_seconds": 5,
            },
            headers=headers("decision"),
        )
        assert response.status_code == 200 and response.json()["answers"]["ok"]["noul"] == 0.9
        assert response.json()["usage"]["cost_usd"] is None
        assert (
            client.post(
                "/decisions",
                json={"model": "typesafe/jev-1.13", "state": {}, "questions": {}},
                headers=headers("unrestricted-model"),
            ).status_code
            == 422
        )


async def test_owner_ipc_requires_verified_private_socket_and_cannot_enable_tcp_bypass(tmp_path):
    import shutil
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="lg-ipc-", dir="/tmp"))
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(root / "gateway.sock"))
    except PermissionError:
        sock.close()
        shutil.rmtree(root)
        pytest.skip("Sandbox denies Unix socket bind; verify this test outside that sandbox")
    (root / "gateway.sock").chmod(0o600)
    engine, _, _ = configured(
        tmp_path, live=False, mutate=lambda d: d.update(access={"owner_only_ipc": True})
    )
    try:
        from llm_gateway.service.auth import Authenticator

        authority = Authenticator(engine.registry, owner_socket=sock, ipc_project="project-a")
        assert authority.authenticate(None, client=None).owner_ipc is True
        app = create_app(engine, owner_socket=sock, ipc_project="project-a")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app, client=None), base_url="http://localhost"
        ) as local:
            assert (await local.get("/ready")).status_code == 200
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app, client=("127.0.0.1", 1000)),
            base_url="http://localhost",
        ) as tcp:
            assert (await tcp.get("/ready")).status_code == 401
    finally:
        sock.close()
        shutil.rmtree(root)


def test_body_limit_and_compression_reject_before_admission(tmp_path):
    client, engine = operation_service(tmp_path, "chat", "public", max_body_bytes=32)
    with client:
        too_big = client.post("/v1/inferences", content="x" * 33, headers=headers("large"))
        assert too_big.status_code == 413 and too_big.headers["X-Gateway-Trace-Id"]
        compressed = client.post(
            "/v1/inferences",
            content=b"bad",
            headers={**headers("compressed"), "Content-Encoding": "gzip"},
        )
        assert compressed.status_code == 415 and compressed.headers["X-Gateway-Trace-Id"]
    assert engine.ledger.report("project-a")["attempts"] == 0


def test_typed_x_http_route_shares_scope_cache_and_receipt_authority(tmp_path, monkeypatch):
    from test_config import write_config
    from test_x_scope import payload, search, x_engine

    from llm_gateway.config import load_project_config

    req = search()
    engine, _, snapshot = x_engine(tmp_path, payload(req), monkeypatch)
    token = private_file(tmp_path, "x.token", "a" * 48)
    data = snapshot.config.model_dump(mode="json")
    data["access"] = {
        "principals": {
            "owner": {
                "token_file": str(token),
                "aliases": ["summary"],
                "operations": ["x_search"],
                "permissions": ["infer", "receipts:own"],
                "run_id": "run-one",
            }
        }
    }
    data["compatibility"] = {"dashboard_profiles": {"dashboard": ["summary"]}}
    engine.registry.activate(
        load_project_config(write_config(tmp_path, data), check_credentials=False)
    )
    wire = {
        "alias": "summary",
        "purpose": "retrieval",
        "profile": "dashboard",
        **req.input.model_dump(mode="json", exclude={"kind", "schema_name"}),
        "response_schema_name": "posts",
    }
    with TestClient(create_app(engine)) as client:
        result = client.post("/v1/x-search", json=wire, headers=headers("x-one"))
        assert result.status_code == 200 and result.json()["provider_metadata"]["scope_verified"]
        reused = client.post("/v1/x-search", json=wire, headers=headers("x-two"))
        assert reused.status_code == 200
        assert (
            reused.json()["provider_metadata"]["receipts"][0]["evidence_status"] == "cached_reuse"
        )
        assert len(engine.provider.calls) == 1
