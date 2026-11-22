"""Private IPC size/failure envelopes preserve exact evidence without inference."""

import io
import json
import socket
import sys
from dataclasses import asdict, replace
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from test_provider_process import prepared
from test_provider_runtime import alias

from llm_gateway.contracts import ChatInput, Message, TokenUsage
from llm_gateway.providers import runtime

pytestmark = pytest.mark.offline


def invoke_response(response, monkeypatch):
    call, provider = prepared()
    calls = []

    def send(request):
        calls.append(request)
        return response

    owned = runtime.ProviderRuntime(
        environment={"ONLY_KEY": "synthetic-private-key"},
        transport_factory=lambda: httpx.MockTransport(send),
    )
    with pytest.raises(runtime.ProviderFailure) as caught:
        owned.invoke(call, provider)
    assert caught.value.dispatched and len(calls) == 1
    assert "synthetic-private-key" not in str(caught.value)
    return caught.value


@pytest.mark.parametrize("kind", ["compression", "oversize", "bad_json", "array"])
def test_bad_upstream_reply_preserves_dispatch_uncertainty_without_retry(monkeypatch, kind):
    if kind == "compression":
        response = httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"never decoded")
        )
    elif kind == "oversize":
        monkeypatch.setattr(runtime, "MAX_RESPONSE_BYTES", 32)
        response = httpx.Response(200, content=b"x" * 33)
    else:
        response = httpx.Response(200, content=b"{" if kind == "bad_json" else b"[]")
    error = invoke_response(response, monkeypatch)
    assert error.evidence is None


def test_http_failure_preserves_observed_decimal_cost(monkeypatch):
    error = invoke_response(
        httpx.Response(429, content=b'{"usage":{"cost":0.000000000000000001}}'), monkeypatch
    )
    assert error.code == "provider_quota"
    assert error.evidence.measured_usd == Decimal("1e-18")


@pytest.mark.parametrize(
    "change", [{"enabled": False}, {"kind": "openrouter"}, {"credential": None}]
)
def test_unavailable_provider_never_constructs_transport(change):
    call, provider = prepared()
    owned = runtime.ProviderRuntime(transport_factory=lambda: pytest.fail("transport constructed"))
    with pytest.raises(runtime.ProviderFailure, match="provider_unavailable") as caught:
        owned.invoke(call, provider.model_copy(update=change))
    assert not caught.value.dispatched


def test_unapproved_sdk_version_fails_before_initialization(monkeypatch):
    monkeypatch.setattr(runtime, "_initialized", False)
    monkeypatch.setattr(runtime.importlib.metadata, "version", lambda name: "unapproved")
    with pytest.raises(RuntimeError, match="unapproved_litellm_version"):
        runtime.initialize()


def test_invalid_provider_identifier_surrogate_is_omitted():
    observed = runtime.evidence({"id": "\ud800", "model": "\udfff", "provider": "safe"}, "secret")
    assert observed.generation_id is None and observed.actual_model is None
    assert observed.actual_provider == "safe"


def test_wire_checker_denies_unapproved_projection_or_nongenerative_output_cap():
    call, _ = prepared()
    with pytest.raises(runtime.ProviderFailure, match="provider_request_invalid"):
        runtime.checked_wire_parameters(call, {}, None)
    with pytest.raises(runtime.ProviderFailure, match="provider_output_bound_missing"):
        runtime.checked_wire_parameters(
            replace(call, max_output_tokens=None), {"max_tokens": 1}, {}
        )


def provider_main(monkeypatch, raw, outcome=None):
    import llm_gateway.isolation.network as network
    from llm_gateway.providers.worker import main

    class Stub:
        def __init__(self, **kwargs):
            pass

        def invoke(self, *args):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    output = io.StringIO()
    monkeypatch.setattr(network, "install", lambda: None)
    monkeypatch.setattr(runtime, "ProviderRuntime", Stub)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))
    monkeypatch.setattr(sys, "stdout", output)
    main()
    return json.loads(output.getvalue())


@pytest.mark.parametrize("raw", [b"{", b"x" * (20 * 1024 * 1024 + 1)])
def test_provider_worker_rejects_bad_or_oversized_protocol_input(monkeypatch, raw):
    assert provider_main(monkeypatch, raw) == {
        "error": "provider_process_invalid",
        "dispatched": False,
    }


def worker_message():
    call, provider = prepared()
    return json.dumps(
        {"prepared": asdict(call), "provider": provider.model_dump(mode="json")}
    ).encode()


def test_provider_worker_failure_without_evidence_stays_nullable(monkeypatch):
    response = provider_main(
        monkeypatch,
        worker_message(),
        runtime.ProviderFailure(
            "provider_failed",
            dispatched=True,
        ),
    )
    assert response == {"error": "provider_failed", "dispatched": True, "evidence": None}


def test_provider_worker_truncates_oversized_output_but_keeps_exact_observed_cost(monkeypatch):
    observed = runtime.ProviderEvidence(
        {"text": "x" * (20 * 1024 * 1024)},
        TokenUsage(),
        Decimal("1e-18"),
        "observed",
        None,
        "generation",
    )
    response = provider_main(monkeypatch, worker_message(), observed)
    assert response["error"] == "provider_response_too_large" and response["dispatched"]
    assert response["evidence"]["payload"] == {}
    assert response["evidence"]["measured_usd"] == "0.000000000000000001"
    assert response["evidence"]["generation_id"] == "generation"


def validation_main(monkeypatch, raw):
    from llm_gateway.service import validation_worker

    # main changes process-wide guards. Register originals with monkeypatch
    # before entry so this serialization-only probe restores all of them.
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, getattr(socket.socket, name))
    monkeypatch.setattr(validation_worker.resource, "setrlimit", lambda *args: None)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))
    monkeypatch.setattr(sys, "stdout", output)
    validation_worker.main()
    return json.loads(output.getvalue())


def validation_message(action):
    return {
        "action": action,
        "alias": alias().model_dump(mode="json"),
        "input": ChatInput(messages=[Message(role="user", content="public")]).model_dump(
            mode="json"
        ),
        "policy": None,
        "payload": {},
    }


def test_validator_rejects_oversized_input_and_unknown_action(monkeypatch):
    assert validation_main(monkeypatch, b"x" * (40 * 1024 * 1024 + 1)) == {
        "error": "validation_failed"
    }
    assert validation_main(monkeypatch, json.dumps(validation_message("unknown")).encode()) == {
        "error": "validation_failed"
    }


def test_validator_blocks_network_and_bounds_serialized_output(monkeypatch):
    from llm_gateway.service import output, validation_worker

    with pytest.raises(PermissionError, match="validation_network_denied"):
        validation_worker.denied()
    monkeypatch.setattr(
        output, "normalize", lambda *args: output.Output("valid", "x" * (20 * 1024 * 1024))
    )
    response = validation_main(monkeypatch, json.dumps(validation_message("normalize")).encode())
    assert response == {"error": "validation_output_too_large"}


def test_cached_validator_bounds_serialized_metadata_and_returns_safe_failure(monkeypatch):
    from test_x_scope import search

    from llm_gateway.config.models import XSearchPolicy

    message = validation_message("cached_x")
    message.update(
        input=search().input.model_dump(mode="json"),
        policy=XSearchPolicy(
            allowed_x_handles=["first", "second"],
            max_handles=2,
            max_utc_days=2,
            max_posts=6,
            max_posts_per_author=2,
            max_post_characters=4000,
            provider_order=["xAI"],
            allowed_domains=["x.com", "twitter.com"],
        ).model_dump(mode="json"),
        document={"posts": []},
        metadata={"oversized": "x" * (20 * 1024 * 1024)},
    )
    response = validation_main(monkeypatch, json.dumps(message).encode())
    assert response == {"error": "validation_output_too_large"}
