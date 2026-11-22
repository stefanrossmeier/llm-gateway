import json
import time
from decimal import Decimal

import pytest
from pydantic import ValidationError
from test_engine import ProviderDouble, configured
from test_ledger import request

from llm_gateway.contracts import InferenceRequest
from llm_gateway.providers.runtime import evidence
from llm_gateway.service.engine import EngineError

pytestmark = pytest.mark.offline


def schema_request(schema):
    return request(
        input={
            "kind": "chat",
            "messages": [{"role": "user", "content": "public"}],
            "response_format": "json_schema",
            "response_schema": schema,
        }
    )


def test_missing_local_reference_fails_before_provider_and_reservation(tmp_path):
    provider = ProviderDouble()
    engine, authority, _ = configured(
        tmp_path,
        provider=provider,
        mutate=lambda d: d["aliases"]["summary"].update(capabilities=["plain_text", "json_schema"]),
    )
    with pytest.raises(EngineError, match="schema_reference_invalid"):
        engine.run(authority, schema_request({"$ref": "#/$defs/missing"}))
    assert provider.calls == []
    assert engine.ledger.report("project-a")["attempts"] == 0


def test_pathological_regex_is_killed_without_losing_charge_or_buying_retry(tmp_path):
    provider = ProviderDouble(content=json.dumps({"value": "a" * 2000 + "!"}))

    def mutate(data):
        profile = data["aliases"]["summary"]
        profile.update(
            capabilities=["plain_text", "json_schema"], attempt_policy={"max_attempts": 3}
        )

    engine, authority, _ = configured(tmp_path, provider=provider, mutate=mutate)
    start = time.monotonic()
    result = engine.run(
        authority,
        schema_request(
            {"type": "object", "properties": {"value": {"type": "string", "pattern": "^(a+)+$"}}}
        ),
    )
    assert time.monotonic() - start < 4
    assert result.output_status == "invalid_document"
    assert "validation_diagnostic" in result.provider_metadata
    assert result.receipts[0].measured_usd == Decimal("0.0001")
    assert len(provider.calls) == 1 and len(result.receipts) == 1
    assert not list(engine.store.root.glob("worker-*"))


def test_invalid_utf8_is_rejected_at_public_contract():
    with pytest.raises(ValidationError, match="UTF-8"):
        InferenceRequest.model_validate(
            {
                "request_id": "one",
                "alias": "summary",
                "operation": "chat",
                "input": {"kind": "chat", "messages": [{"role": "user", "content": "\ud800"}]},
            }
        )


def test_small_scientific_json_cost_is_exact_observed_evidence():
    payload = json.loads('{"usage":{"cost":4.2e-7}}', parse_float=Decimal)
    observed = evidence(payload, "synthetic-key")
    assert observed.measured_usd == Decimal("0.00000042") and not observed.billing_anomaly


@pytest.mark.parametrize("failure", [False, True])
def test_provider_worker_ipc_serializes_small_cost_as_canonical_decimal(monkeypatch, failure):
    import io
    import sys
    from dataclasses import asdict

    from test_provider_process import prepared

    import llm_gateway.isolation.network as network
    import llm_gateway.providers.runtime as runtime
    from llm_gateway.contracts import TokenUsage
    from llm_gateway.providers.process import decode_evidence
    from llm_gateway.providers.worker import main

    observed = runtime.ProviderEvidence({}, TokenUsage(), Decimal("1e-18"), None, None, None)

    class Stub:
        def __init__(self, **kwargs):
            pass

        def invoke(self, *args):
            if failure:
                raise runtime.ProviderFailure("provider_failed", dispatched=True, evidence=observed)
            return observed

    call, provider = prepared()
    source = io.TextIOWrapper(
        io.BytesIO(
            json.dumps(
                {"prepared": asdict(call), "provider": provider.model_dump(mode="json")}
            ).encode()
        )
    )
    output = io.StringIO()
    monkeypatch.setattr(runtime, "ProviderRuntime", Stub)
    monkeypatch.setattr(network, "install", lambda: None)
    monkeypatch.setattr(sys, "stdin", source)
    monkeypatch.setattr(sys, "stdout", output)
    main()
    response = json.loads(output.getvalue())
    assert response["evidence"]["measured_usd"] == "0.000000000000000001"
    assert decode_evidence(response["evidence"]).measured_usd == Decimal("1e-18")
