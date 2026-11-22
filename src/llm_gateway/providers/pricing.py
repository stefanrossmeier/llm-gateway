"""Local reviewed rate evidence only; estimates never become measured billing."""

from decimal import Decimal, localcontext

from llm_gateway.config.models import AliasConfig
from llm_gateway.contracts import TokenUsage, parse_usd


class PricingError(ValueError):
    pass


def reserve_usd(
    alias: AliasConfig,
    input_bound: int,
    output_bound: int | None,
    *,
    provider_kind: str | None = None,
) -> Decimal:
    if alias.operation == "x_search" or "native_x_search" in alias.capabilities:
        if provider_kind != "openrouter":
            raise PricingError("native_x_provider_unsupported")
        pricing = alias.pricing
        if pricing is None or pricing.free_only:
            raise PricingError("pricing_bound_unavailable")
        if not pricing.accept_unbounded_x_charges:
            raise PricingError("native_x_risk_acceptance_required")
        if (
            pricing.x_post_usd is None
            or pricing.x_profile_usd is None
            or pricing.input_usd_per_token is None
            or pricing.output_usd_per_token is None
            or pricing.x_request_allowance_usd is None
            or pricing.x_request_allowance_usd <= 0
        ):
            raise PricingError("native_x_pricing_incomplete")
        # Native search bills every fetched post/profile, plus cumulative agentic
        # tokens. A returned-result or tool-call count cannot bound those units.
        # This is explicitly accepted exposure, never an enforced provider cap.
        return pricing.x_request_allowance_usd
    pricing = alias.pricing
    if pricing is None:
        raise PricingError("pricing_bound_unavailable")
    if pricing.free_only:
        return Decimal("0")
    if alias.operation == "decisions" and (
        pricing.output_usd_per_token != Decimal("0")
        or pricing.reasoning_usd_per_token != Decimal("0")
    ):
        # The pinned Decisions API has no enforceable output-token limit. A
        # configured number alone is not a provider-enforced monetary ceiling.
        # Input-only models can be bounded without charging any output unit.
        raise PricingError("decision_output_bound_unavailable")
    if pricing.input_bound is None or pricing.framing_tokens_per_item is None:
        raise PricingError("pricing_bound_unavailable")
    if pricing.input_usd_per_token is None:
        raise PricingError("pricing_bound_unavailable")
    if alias.operation != "embeddings" and pricing.output_usd_per_token is None:
        raise PricingError("pricing_bound_unavailable")
    with localcontext() as context:
        context.prec = 50
        rate = max(
            pricing.output_usd_per_token or Decimal("0"),
            pricing.reasoning_usd_per_token or Decimal("0"),
        )
        input_rate = max(
            pricing.input_usd_per_token,
            pricing.cached_input_usd_per_token or pricing.input_usd_per_token,
        )
        result = input_bound * input_rate + (output_bound or 0) * rate
        if pricing.ceiling_usd is not None and result > pricing.ceiling_usd:
            raise PricingError("configured_ceiling_not_defensible")
        try:
            return parse_usd(result)
        except ValueError:
            raise PricingError("price_precision_or_amount_unsupported") from None


def estimate_usd(alias: AliasConfig, usage: TokenUsage) -> Decimal | None:
    if alias.operation == "x_search" or "native_x_search" in alias.capabilities:
        pricing = alias.pricing
        if (
            pricing is None
            or pricing.free_only
            or pricing.x_post_usd is None
            or pricing.x_profile_usd is None
            or pricing.input_usd_per_token is None
            or pricing.output_usd_per_token is None
            or usage.input_tokens is None
            or usage.output_tokens is None
            or usage.x_posts_fetched is None
            or usage.x_profiles_fetched is None
        ):
            return None
        with localcontext() as context:
            context.prec = 50
            amount = (
                usage.input_tokens * pricing.input_usd_per_token
                + usage.output_tokens
                * max(pricing.output_usd_per_token, pricing.reasoning_usd_per_token or Decimal("0"))
                + usage.x_posts_fetched * pricing.x_post_usd
                + usage.x_profiles_fetched * pricing.x_profile_usd
            )
            try:
                return parse_usd(amount)
            except ValueError:
                return None
    pricing = alias.pricing
    if pricing is None or pricing.free_only or usage.input_tokens is None:
        return None
    if pricing.input_usd_per_token is None:
        return None
    if alias.operation == "decisions" and (
        pricing.output_usd_per_token != Decimal("0")
        or pricing.reasoning_usd_per_token != Decimal("0")
    ):
        return None
    if alias.operation not in {"embeddings", "decisions"} and (
        usage.output_tokens is None or pricing.output_usd_per_token is None
    ):
        return None
    with localcontext() as context:
        context.prec = 50
        # With absent cache/reasoning splits, use the conservative visible rate;
        # absent required measurements are not fabricated as zero.
        output_rate = max(
            pricing.output_usd_per_token or Decimal("0"),
            pricing.reasoning_usd_per_token or Decimal("0"),
        )
        input_rate = max(
            pricing.input_usd_per_token,
            pricing.cached_input_usd_per_token or pricing.input_usd_per_token,
        )
        amount = usage.input_tokens * input_rate + (usage.output_tokens or 0) * output_rate
        try:
            return parse_usd(amount)
        except ValueError:
            return None
