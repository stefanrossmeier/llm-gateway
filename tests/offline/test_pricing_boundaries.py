"""COST admission cannot fabricate a bound from partial/overflowing price evidence."""

from decimal import Decimal

import pytest
from test_provider_runtime import alias

from llm_gateway.config.models import PricingConfig
from llm_gateway.contracts import TokenUsage
from llm_gateway.providers.pricing import PricingError, estimate_usd, reserve_usd

pytestmark = pytest.mark.offline


def profile(**changes):
    base = alias()
    pricing = base.pricing.model_dump(mode="json") | changes
    return base.model_copy(update={"pricing": PricingConfig.model_validate(pricing)})


@pytest.mark.parametrize(
    "field",
    [
        "input_bound",
        "framing_tokens_per_item",
        "input_usd_per_token",
        "output_usd_per_token",
    ],
)
def test_missing_price_bound_component_blocks_reservation(field):
    with pytest.raises(PricingError, match="pricing_bound_unavailable"):
        reserve_usd(profile(**{field: None}), 10, 20)


def test_no_pricing_is_not_a_free_reservation():
    with pytest.raises(PricingError, match="pricing_bound_unavailable"):
        reserve_usd(alias().model_copy(update={"pricing": None}), 10, 20)


def test_declared_ceiling_cannot_replace_the_defensible_bound():
    with pytest.raises(PricingError, match="configured_ceiling_not_defensible"):
        reserve_usd(profile(ceiling_usd="0.000001"), 10, 20)


def test_overflowing_reservation_fails_and_estimate_stays_unavailable():
    selected = profile(input_usd_per_token="1000000000")
    with pytest.raises(PricingError, match="price_precision_or_amount_unsupported"):
        reserve_usd(selected, 2, 1)
    assert estimate_usd(selected, TokenUsage(input_tokens=2, output_tokens=1)) is None


@pytest.mark.parametrize("missing", ["pricing", "input_rate", "output_rate", "input", "output"])
def test_missing_usage_or_rate_is_never_estimated_as_zero(missing):
    selected, usage = alias(), TokenUsage(input_tokens=10, output_tokens=20)
    if missing == "pricing":
        selected = selected.model_copy(update={"pricing": None})
    elif missing.endswith("rate"):
        selected = profile(
            **{"input_usd_per_token" if missing == "input_rate" else "output_usd_per_token": None}
        )
    else:
        usage = TokenUsage.model_validate(
            {
                "input_tokens": None if missing == "input" else 10,
                "output_tokens": None if missing == "output" else 20,
            }
        )
    assert estimate_usd(selected, usage) is None


def test_conservative_cache_and_reasoning_evidence_are_counted_once():
    selected = profile(cached_input_usd_per_token="0.000003", reasoning_usd_per_token="0.000005")
    usage = TokenUsage(input_tokens=10, output_tokens=20, cache_tokens=4, reasoning_tokens=12)
    assert reserve_usd(selected, 10, 20) == Decimal("0.00013")
    assert estimate_usd(selected, usage) == Decimal("0.00013")


def x_alias(**changes):
    selected = profile(
        x_post_usd="0.005",
        x_profile_usd="0.01",
        x_request_allowance_usd="0.25",
        accept_unbounded_x_charges=True,
        **changes,
    )
    return selected.model_copy(
        update={"operation": "x_search", "capabilities": ["native_x_search", "json_schema"]}
    )


def test_x_reserves_explicit_risk_allowance_not_an_invented_per_call_bound():
    selected = x_alias()
    assert reserve_usd(selected, 10, 20, provider_kind="openrouter") == Decimal("0.25")
    for calls in [1, 64]:
        changed = selected.model_copy(
            update={"limits": selected.limits.model_copy(update={"max_tool_calls": calls})}
        )
        assert reserve_usd(changed, 10, 20, provider_kind="openrouter") == Decimal("0.25")
    with pytest.raises(PricingError, match="native_x_provider_unsupported"):
        reserve_usd(selected, 10, 20, provider_kind="openai")


def test_legacy_flat_fee_never_establishes_x_admission_authority():
    selected = profile(tool_fees_usd={"native_x_search": "100"}, tool_fee_calls=64)
    selected = selected.model_copy(update={"operation": "x_search"})
    with pytest.raises(PricingError, match="native_x_risk_acceptance_required"):
        reserve_usd(selected, 10, 20, provider_kind="openrouter")


@pytest.mark.parametrize(
    "missing",
    [
        "x_post_usd",
        "x_profile_usd",
        "input_usd_per_token",
        "output_usd_per_token",
        "x_request_allowance_usd",
    ],
)
def test_incomplete_x_pricing_cannot_admit(missing):
    selected = x_alias()
    selected = selected.model_copy(
        update={"pricing": selected.pricing.model_copy(update={missing: None})}
    )
    with pytest.raises(PricingError, match="native_x_pricing_incomplete"):
        reserve_usd(selected, 10, 20, provider_kind="openrouter")


def test_x_estimate_uses_fetched_posts_and_profiles_never_search_count():
    selected = x_alias()
    usage = TokenUsage(input_tokens=10, output_tokens=20, x_posts_fetched=12, x_profiles_fetched=2)
    assert estimate_usd(selected, usage) == Decimal("0.08005")
    assert estimate_usd(selected, TokenUsage(input_tokens=10, output_tokens=20)) is None
    assert estimate_usd(selected, usage.model_copy(update={"x_profiles_fetched": None})) is None
    assert estimate_usd(selected, usage.model_copy(update={"input_tokens": None})) is None


def test_x_without_pricing_is_not_free_and_overflow_does_not_invent_cost():
    selected = x_alias()
    unpriced = selected.model_copy(update={"pricing": None})
    with pytest.raises(PricingError, match="pricing_bound_unavailable"):
        reserve_usd(unpriced, 10, 20, provider_kind="openrouter")
    assert estimate_usd(unpriced, TokenUsage()) is None
    huge = selected.model_copy(
        update={
            "pricing": selected.pricing.model_copy(update={"x_post_usd": Decimal("1000000000")})
        }
    )
    assert (
        estimate_usd(
            huge,
            TokenUsage(input_tokens=1, output_tokens=1, x_posts_fetched=100, x_profiles_fetched=0),
        )
        is None
    )
