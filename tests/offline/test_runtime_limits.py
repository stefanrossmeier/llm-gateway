"""Specified deployment limits apply independently of project retention policy."""

from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from test_api import auth

from llm_gateway.client import AsyncGatewayClient, GatewayClient
from llm_gateway.service import api, cli, server
from llm_gateway.service.engine import Engine, EngineError
from llm_gateway.service.output import Output
from llm_gateway.storage import StateStore

pytestmark = pytest.mark.offline
pytest_plugins = ["test_api", "test_live_runner"]


def test_engine_default_and_hard_capacity_limits(tmp_path):
    engine = Engine(StateStore(tmp_path / "default"))
    assert engine.max_concurrency == 8 and engine.max_queue == 256
    maximum = Engine(StateStore(tmp_path / "maximum"), max_concurrency=64, max_queue=1024)
    assert maximum.max_concurrency == 64 and maximum.max_queue == 1024


@pytest.mark.parametrize(
    "values",
    [
        {"max_concurrency": True},
        {"max_concurrency": 1.5},
        {"max_concurrency": None},
        {"max_queue": False},
        {"max_queue": 1025},
    ],
)
def test_invalid_capacities_cannot_construct_an_engine(values):
    with pytest.raises(EngineError, match="engine_capacity_invalid"):
        Engine(None, **values)


@pytest.mark.parametrize("value", [0, True, 1.5, None, api.MAX_BODY + 1])
def test_invalid_body_limits_fail_before_binding_or_state_creation(
    service, tmp_path, monkeypatch, value
):
    _, engine, _, _ = service
    with pytest.raises(EngineError, match="http_body_limit_invalid"):
        api.create_app(engine, max_body_bytes=value)
    monkeypatch.setattr(
        server.socket, "socket", lambda *args: pytest.fail("invalid limit bound socket")
    )
    root = tmp_path / "not-created"
    with pytest.raises(EngineError, match="http_body_limit_invalid"):
        server.serve(project_paths=[], state_root=root, max_body_bytes=value)
    assert not root.exists()


def test_default_body_limit_can_be_explicitly_tightened_or_raised(service):
    _, engine, _, _ = service
    body = {"unrecognized": "x" * api.DEFAULT_BODY}
    for value, status in [(api.DEFAULT_BODY, 413), (2 * api.DEFAULT_BODY, 422), (32, 413)]:
        with TestClient(api.create_app(engine, max_body_bytes=value)) as client:
            response = client.post(
                "/v1/inferences", json=body, headers=auth(identifier="body-limit")
            )
        assert response.status_code == status
    api.create_app(engine, max_body_bytes=api.MAX_BODY)
    assert engine.ledger.report("project-a")["attempts"] == 0


@pytest.mark.parametrize("custom", [False, True])
def test_cli_forwards_explicit_bounded_operator_settings(monkeypatch, custom):
    observed = []
    monkeypatch.setattr(server, "serve", lambda **kwargs: observed.append(kwargs))
    args = ["serve", "--project-config", "/unused", "--state-dir", "/unused"]
    if custom:
        args += ["--max-concurrency", "4", "--max-queue", "12", "--max-body-bytes", "2000"]
    assert cli.main(args) == 0
    assert {
        name: observed[0][name] for name in ("max_concurrency", "max_queue", "max_body_bytes")
    } == {
        "max_concurrency": 4 if custom else 8,
        "max_queue": 12 if custom else 256,
        "max_body_bytes": 2000 if custom else api.DEFAULT_BODY,
    }


@pytest.mark.parametrize("live_double", [True], indirect=True)
@pytest.mark.parametrize("path", ["/v1/inferences", "/v1/chat/completions"])
def test_oversized_delivery_keeps_exact_settled_receipt_without_redispatch(
    live_double, monkeypatch, path
):
    plan, snapshot, _, engine, _, _ = live_double
    engine.provider.cost = Decimal("0.0001")  # Inference double, never a paid provider.
    assert snapshot.config.evidence_policy.retain_results is False
    # Fault-inject a validator result at the delivery boundary. Duplication of
    # function arguments/ordered metadata can enlarge otherwise bounded captures.
    monkeypatch.setattr(
        engine.validation,
        "normalize",
        lambda *args, **kwargs: Output(
            "valid",
            "x" * api.MAX_RESULT,
        ),
    )
    canonical = plan.cases[0].request
    body = (
        canonical.model_dump(mode="json")
        if path == "/v1/inferences"
        else {
            "model": canonical.alias,
            "messages": [{"role": "user", "content": "public"}],
        }
    )
    headers = {"Authorization": "Bearer " + "t" * 48, "Idempotency-Key": canonical.request_id}
    with TestClient(api.create_app(engine)) as client:
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "result_output_too_large"
        assert response.headers["X-Gateway-Output-Status"] == "unavailable"
        assert len(response.content) < api.MAX_RESULT
        receipt = response.json()["receipts"][0]
        assert receipt["measured_usd"] == "0.0001"
        assert receipt["receipt_id"] == response.headers["X-Gateway-Receipt-Id"]
        recovered = client.get("/v1/requests/" + canonical.request_id, headers=headers)
    assert recovered.status_code == 410
    assert recovered.json()["receipts"][0] == receipt
    assert len(engine.provider.calls) == 1
    assert (
        engine.ledger.request(snapshot.project_id, plan.principal_id, canonical.request_id)[
            "execution_status"
        ]
        == "completed"
    )


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_client_connect_and_pool_timeouts_match_the_specification(asynchronous):
    transport = httpx.MockTransport(lambda request: pytest.fail("timeout probe used transport"))
    if asynchronous:
        async with AsyncGatewayClient(
            "https://gateway.test", token="a" * 48, transport=transport
        ) as client:
            assert client._http.timeout.connect == 5 and client._http.timeout.pool == 10
    else:
        with GatewayClient("https://gateway.test", token="a" * 48, transport=transport) as client:
            assert client._http.timeout.connect == 5 and client._http.timeout.pool == 10
