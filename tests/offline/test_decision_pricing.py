from decimal import Decimal

import httpx
import pytest
from test_engine import configured
from test_provider_runtime import alias

from llm_gateway.config.models import DecisionsPolicy, ProviderConfig
from llm_gateway.contracts import DecisionsInput, InferenceRequest, TokenUsage
from llm_gateway.providers.preparation import prepare
from llm_gateway.providers.pricing import PricingError, estimate_usd, reserve_usd
from llm_gateway.providers.runtime import ProviderRuntime, initialize
from llm_gateway.service.engine import EngineError

pytestmark = pytest.mark.offline


def input_only_alias():
    data = alias("decisions").model_dump(mode="json")
    data["pricing"].update(output_usd_per_token="0", reasoning_usd_per_token="0")
    return type(alias()).model_validate(data)


def test_input_only_prices_do_not_need_an_unenforceable_output_cap():
    profile = input_only_alias()
    assert profile.limits.max_output_tokens is None
    assert reserve_usd(profile, 100, None) == Decimal("0.000100")
    assert estimate_usd(profile, TokenUsage(input_tokens=10)) == Decimal("0.000010")
    assert estimate_usd(profile, TokenUsage(input_tokens=10, output_tokens=9000)) == Decimal(
        "0.000010"
    )
    with pytest.raises(PricingError, match="decision_output_bound_unavailable"):
        reserve_usd(alias("decisions"), 100, None)


def test_state_accounting_is_consumer_owned_and_default_repeats_per_question():
    profile = input_only_alias()
    data = DecisionsInput(
        state={"text": "public"}, questions={"first": {"type": "noul"}, "second": {"type": "noul"}}
    )
    provider = ProviderConfig(kind="openrouter")
    default = prepare(profile, provider, data)
    shared = prepare(
        profile,
        provider,
        data,
        operation_policy=DecisionsPolicy(max_questions=2, state_accounting="shared_once"),
    )
    assert default.input_token_bound == 2 * shared.input_token_bound
    assert shared.input_token_bound >= 2 * len(shared.body_json.encode())


def test_actual_sdk_pipeline_admits_input_only_decisions_with_estimated_receipt(tmp_path):
    # This test proves pricing/translation, not cold parent SDK import latency.
    # Real private worker startup remains inside production deadlines elsewhere.
    initialize()
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "id": "decision-fixture",
                "model": "fixture-actual",
                "answers": {
                    "first": {"type": "noul", "noul": 0.9},
                    "second": {"type": "noul", "noul": 0.2},
                },
                "usage": {"input_tokens": 10, "output_tokens": 8},
            },
        )

    runtime = ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(send),
        environment={"FIXTURE_KEY": "synthetic-key"},
    )

    class Adapter:
        def invoke(self, prepared, provider, **kwargs):
            return runtime.invoke(prepared, provider)

    def mutate(data):
        profile = data["aliases"]["summary"]
        profile.update(
            operation="decisions",
            capabilities=[],
            operation_policy_ref="classify",
            upstream_model="fixture/decision",
        )
        profile["limits"].update(
            max_output_tokens=None,
            max_input_tokens=65536,
            attempt_timeout_seconds=5,
            request_timeout_seconds=10,
        )
        profile["pricing"].update(output_usd_per_token="0", reasoning_usd_per_token="0")
        data["operation_policies"] = {
            "classify": {"kind": "decisions", "max_questions": 2, "state_accounting": "shared_once"}
        }

    engine, authority, _ = configured(tmp_path, provider=Adapter(), mutate=mutate)
    authority = type(authority)(
        authority.project,
        authority.principal,
        authority.grants.model_copy(update={"operations": ["decisions"]}),
    )
    request = InferenceRequest.model_validate(
        {
            "request_id": "one",
            "alias": "summary",
            "operation": "decisions",
            "input": {
                "kind": "decisions",
                "state": {"text": "public"},
                "questions": {
                    "first": {"type": "noul", "instructions": "first?"},
                    "second": {"type": "noul", "instructions": "second?"},
                },
            },
        }
    )
    result = engine.run(authority, request)
    assert result.output_status == "valid", result.provider_metadata
    assert result.content["answers"]["first"]["noul"] == 0.9
    assert result.receipts[0].estimated_usd == Decimal("0.000010")
    assert result.receipts[0].measured_usd is None
    assert result.receipts[0].protected_debit_usd > result.receipts[0].estimated_usd
    with pytest.raises(EngineError, match="decision_output_limit_unsupported"):
        engine.run(
            authority,
            request.model_copy(
                update={
                    "request_id": "invalid-limit",
                    "limits": request.limits.model_copy(update={"max_output_tokens": 16}),
                }
            ),
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "legacy",
    [
        "litellm-1.109.0/gateway-v1",
        "litellm-1.109.0/gateway-v2",
        "litellm-1.109.0/gateway-v3",
        "litellm-1.109.0/gateway-v4",
        "litellm-1.109.0/gateway-v5",
    ],
)
def test_prior_capability_snapshots_remain_readable_but_require_reload_for_new_calls(
    tmp_path, legacy
):
    import hashlib
    import json

    from test_config import write_config

    from llm_gateway.config import load_project_config
    from llm_gateway.config.loader import ConfigSnapshot, canonical_json

    engine, authority, snapshot = configured(tmp_path, live=False)
    data = json.loads(snapshot.payload)
    for provider in data["providers"].values():
        for field in (
            "base_url",
            "authentication",
            "operations",
            "free_models",
            "free_model_suffixes",
        ):
            provider.pop(field, None)
    data["aliases"]["summary"].update(operation="decisions", capabilities=[])
    digest = hashlib.sha256(
        canonical_json(
            {"project_config": data, "fixtures": snapshot.fixtures, "capability_revision": legacy}
        ).encode()
    ).hexdigest()
    previous = ConfigSnapshot(
        snapshot.project_id, digest, canonical_json(data), snapshot.fixtures_payload, legacy
    )
    engine.registry.activate(previous)
    authority = type(authority)(
        authority.project,
        authority.principal,
        authority.grants.model_copy(update={"operations": ["decisions"]}),
    )
    request = InferenceRequest.model_validate(
        {
            "request_id": "historical",
            "alias": "summary",
            "operation": "decisions",
            "context": {"run_id": "run-one"},
            "input": {"kind": "decisions", "state": {}, "questions": {"ok": {"type": "noul"}}},
        }
    )
    engine.ledger.register(authority.project, authority.principal, request, previous)
    attempt = engine.ledger.admit(
        authority.project,
        authority.principal,
        request.request_id,
        ordinal=1,
        route={"provider_ref": "router", "upstream_model": "fixture-model"},
        reserve=Decimal("0"),
        buckets=[],
        max_attempts=1,
    )
    engine.ledger.settle(authority.project, attempt, status="simulated")
    engine.ledger.finish(
        authority.project,
        authority.principal,
        request.request_id,
        execution_status="completed",
        output_status="valid",
    )
    assert engine.lookup(authority, request.request_id).config_digest == digest
    with pytest.raises(EngineError, match="reload_required"):
        engine.run(authority, request.model_copy(update={"request_id": "new"}))
    data["aliases"]["summary"]["limits"]["max_output_tokens"] = None
    current = load_project_config(write_config(tmp_path, data))
    engine.registry.activate(current)
    assert current.digest != digest
    assert engine.run(authority, request).receipts[0].config_digest == digest
    assert (
        engine.registry.get(authority.project, digest)
        .config.aliases["summary"]
        .limits.max_output_tokens
        == 64
    )
