"""Bounded diagnostic codes without raw upstream text or credential disclosure."""

import json
from decimal import Decimal

import httpx
import pytest
from test_provider_process import prepared
from test_provider_runtime import alias

from llm_gateway.config.models import ProviderConfig
from llm_gateway.contracts import ChatInput, Message
from llm_gateway.providers import runtime
from llm_gateway.providers.preparation import prepare

pytestmark = pytest.mark.offline


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, "provider_http_400"),
        (402, "provider_http_402"),
        (404, "provider_http_404"),
        (422, "provider_http_422"),
        (500, "provider_http_500"),
        (503, "provider_http_503"),
        (401, "provider_auth"),
        (403, "provider_auth"),
        (429, "provider_quota"),
    ],
)
def test_http_status_survives_with_no_raw_error_or_secret(status, expected):
    call, provider = prepared()
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={
                "error": {"message": "synthetic-secret private-prompt https://private.invalid/"},
                "usage": {"cost": "0.000001"},
            },
        )

    owned = runtime.ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(send),
        environment={"ONLY_KEY": "synthetic-secret"},
    )
    with pytest.raises(runtime.ProviderFailure) as caught:
        owned.invoke(call, provider)
    error = caught.value
    assert error.code == expected and error.dispatched
    assert str(error) == expected and len(calls) == 1
    assert error.evidence.measured_usd == Decimal("0.000001")
    # Only the classified code crosses the failure channel, never response text.
    assert "private-prompt" not in str(error)
    assert "synthetic-secret" not in json.dumps(error.evidence.payload)


def test_transport_failure_is_distinct_from_failure_before_http():
    call, provider = prepared()

    def send(request):
        raise httpx.ConnectError("synthetic-secret private-network", request=request)

    owned = runtime.ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(send),
        environment={"ONLY_KEY": "synthetic-secret"},
    )
    with pytest.raises(runtime.ProviderFailure) as caught:
        owned.invoke(call, provider)
    assert caught.value.code == "provider_transport_failed"
    assert caught.value.dispatched and caught.value.evidence is None


def test_sdk_failure_before_dispatch_does_not_echo_exception(monkeypatch):
    call, provider = prepared()
    runtime.initialize()
    import litellm

    def reject(**kwargs):
        raise ValueError("synthetic-secret private-prompt")

    monkeypatch.setattr(litellm, "completion", reject)
    owned = runtime.ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(lambda request: pytest.fail("HTTP called")),
        environment={"ONLY_KEY": "synthetic-secret"},
    )
    with pytest.raises(runtime.ProviderFailure) as caught:
        owned.invoke(call, provider)
    assert caught.value.code == "provider_before_dispatch_failed"
    assert not caught.value.dispatched and caught.value.evidence is None


@pytest.mark.parametrize(
    "message,reason",
    [
        ("No endpoints found that support tool use.", "tools_unavailable"),
        ("No endpoints found that can handle the requested parameters.", "parameters_unavailable"),
        ("No endpoints found matching your data policy.", "data_policy"),
        ("No endpoints found supporting your data region.", "data_region"),
    ],
)
def test_only_known_openrouter_routing_reason_crosses_error_channel(message, reason):
    provider = ProviderConfig(kind="openrouter", credential={"env_var": "ONLY_KEY"})
    call = prepare(
        alias(model="fixture/model"),
        provider,
        ChatInput(messages=[Message(role="user", content="public")]),
    )
    owned = runtime.ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(
            lambda request: httpx.Response(
                404,
                json={"error": {"message": message + " synthetic-secret private-prompt"}},
            )
        ),
        environment={"ONLY_KEY": "synthetic-secret"},
    )
    with pytest.raises(runtime.ProviderFailure) as caught:
        owned.invoke(call, provider)
    assert str(caught.value) == f"provider_http_404_{reason}"
    assert caught.value.dispatched


@pytest.mark.parametrize("error", [None, [], {"message": []}, {"message": "private-prompt"}])
def test_unknown_or_malformed_router_error_does_not_become_a_diagnostic(error):
    provider = ProviderConfig(kind="openrouter", credential={"env_var": "ONLY_KEY"})
    call = prepare(
        alias(model="fixture/model"),
        provider,
        ChatInput(messages=[Message(role="user", content="public")]),
    )
    owned = runtime.ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(
            lambda request: httpx.Response(
                404,
                json={"error": error},
            )
        ),
        environment={"ONLY_KEY": "synthetic-secret"},
    )
    with pytest.raises(runtime.ProviderFailure) as caught:
        owned.invoke(call, provider)
    assert str(caught.value) == "provider_http_404"
