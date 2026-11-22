"""ARCH-10 / T021: real pinned adapters, simultaneous independent project routes."""

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from test_config import private_file, write_config
from test_provider_runtime import alias, payload

from llm_gateway.config import load_project_config
from llm_gateway.providers.runtime import ProviderRuntime
from llm_gateway.service.api import create_app
from llm_gateway.service.engine import Engine
from llm_gateway.storage import StateStore

pytestmark = pytest.mark.offline


def test_simultaneous_live_adapter_keys_cache_budgets_and_readiness_never_cross(
    tmp_path, monkeypatch
):
    calls, lock = [], threading.Lock()

    def send(request):
        with lock:
            calls.append((request.url.host, request.headers["authorization"]))
        return httpx.Response(200, json=payload("chat"))

    runtime = ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(send),
        environment={"PROJECT_A_KEY": "synthetic-a", "PROJECT_B_KEY": "synthetic-b"},
    )

    class Adapter:
        def invoke(self, prepared, provider, **kwargs):
            return runtime.invoke(prepared, provider)

    engine = Engine(StateStore(tmp_path / "state"), provider=Adapter())
    for letter, kind, model in [
        ("a", "openai", "gpt-4o-mini"),
        ("b", "openrouter", "openrouter/free"),
    ]:
        token = private_file(tmp_path, letter + ".token", letter * 48)
        key_name = "PROJECT_" + letter.upper() + "_KEY"
        monkeypatch.setenv(key_name, "synthetic-" + letter)
        profile = alias(model=model).model_dump(mode="json")
        profile["budget_policy_refs"] = ["call", "run"]
        # Two real validation subprocesses start concurrently under coverage.
        # This tests isolation/authority, while deadline behavior has its own
        # adversarial tests; a two-second startup race is not the contract here.
        profile["limits"].update(attempt_timeout_seconds=5, request_timeout_seconds=10)
        data = {
            "schema_version": 1,
            "project_id": "project-" + letter,
            "mode": "live",
            "providers": {
                "provider": {"kind": kind, "credential": {"env_var": key_name}},
                "disabled": {
                    "kind": "openai",
                    "enabled": False,
                    "credential": {"env_var": "UNRELATED_MISSING_KEY"},
                },
            },
            "aliases": {"summary": profile},
            "budgets": {
                "call": {"scope": "project", "period": "call", "cap_usd": "1"},
                "run": {"scope": "project", "period": "run", "cap_usd": "1"},
            },
            "access": {
                "principals": {
                    "owner": {
                        "token_file": str(token),
                        "aliases": ["summary"],
                        "operations": ["chat"],
                        "permissions": ["infer", "receipts:own", "usage:project"],
                        "run_id": "bound-run",
                    }
                }
            },
            "evidence_policy": {
                "retain_results": True,
                "result_ttl_seconds": 60,
                "cache_enabled": True,
            },
        }
        engine.registry.activate(load_project_config(write_config(tmp_path, data)))
    with TestClient(create_app(engine)) as client:

        def inference(letter, identifier):
            return client.post(
                "/v1/inferences",
                json={
                    "request_id": identifier,
                    "alias": "summary",
                    "operation": "chat",
                    "input": {"kind": "chat", "messages": [{"role": "user", "content": "public"}]},
                },
                headers={"Authorization": "Bearer " + letter * 48},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = list(pool.map(lambda letter: inference(letter, "one"), ["a", "b"]))
        assert a.status_code == b.status_code == 200
        assert set(calls) == {
            ("api.openai.com", "Bearer synthetic-a"),
            ("openrouter.ai", "Bearer synthetic-b"),
        }
        assert a.json()["config_digest"] != b.json()["config_digest"]
        assert a.json()["model"]["upstream_model"] == "gpt-4o-mini"
        assert b.json()["model"]["upstream_model"] == "openrouter/free"
        assert inference("a", "two").json()["receipts"][0]["evidence_status"] == "cached_reuse"
        assert inference("b", "two").json()["receipts"][0]["evidence_status"] == "cached_reuse"
        assert len(calls) == 2
        assert engine.ledger.report("project-a")["measured_usd"] == "0.001"
        assert engine.ledger.report("project-b")["measured_usd"] == "0.001"
        assert (
            client.get(
                "/v1/receipts/" + a.headers["X-Gateway-Receipt-Id"],
                headers={"Authorization": "Bearer " + "b" * 48},
            ).status_code
            == 404
        )
        monkeypatch.delenv("PROJECT_B_KEY")
        assert (
            client.get("/ready", headers={"Authorization": "Bearer " + "a" * 48}).status_code == 200
        )
        assert (
            client.get("/ready", headers={"Authorization": "Bearer " + "b" * 48}).status_code == 503
        )
        assert len(calls) == 2
