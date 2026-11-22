"""Security/cost configuration regressions: reject authority/route/fee expansion."""

import copy

import pytest
from pydantic import ValidationError
from test_paid_proposal import project

from llm_gateway.config.models import ProjectConfig

pytestmark = pytest.mark.offline


SCENARIOS = [
    "model_control",
    "empty_credential",
    "relative_credential",
    "relative_token",
    "deadline_inversion",
    "provider_storage",
    "unknown_fee",
    "free_paid_price",
    "chat_native_fee",
    "decision_output_cap",
    "nonchat_unknown_retry",
    "unknown_retry_fallback",
    "capability_expansion",
    "format_without_grant",
    "reasoning_without_grant",
    "verbosity_without_grant",
    "missing_output_cap",
    "duplicate_handles",
    "cache_without_retention",
    "missing_auth",
    "disabled_provider",
    "missing_credential",
    "model_endpoint",
    "wrong_provider_prefix",
    "missing_budget",
    "missing_policy",
    "missing_pricing",
    "no_run_cap",
    "override_unknown_alias",
    "principal_unknown_alias",
    "compat_unknown_alias",
    "fixture_unknown_alias",
    "fallback_missing_provider",
    "fallback_disabled",
    "fallback_endpoint",
    "fallback_wrong_provider",
    "fallback_free_paid_route",
    "fallback_lost_capability",
]


def configuration(tmp_path, scenario):
    data = project(tmp_path).config.model_dump(mode="json")
    alias = data["aliases"]["summary"]
    changes = {
        "model_control": (alias, "upstream_model", "synthetic model"),
        "empty_credential": (data["providers"]["router"], "credential", {}),
        "relative_credential": (data["providers"]["router"], "credential", {"file_path": "key"}),
        "relative_token": (data["access"]["principals"]["test"], "token_file", "token"),
        "deadline_inversion": (alias["limits"], "attempt_timeout_seconds", 3),
        "provider_storage": (alias["generation"], "store", True),
        "unknown_fee": (alias["pricing"], "tool_fees_usd", {"unknown": "0.01"}),
        "free_paid_price": (alias["pricing"], "free_only", True),
        "chat_native_fee": (alias["limits"], "max_tool_calls", 1),
        "capability_expansion": (alias, "capabilities", ["native_x_search"]),
        "format_without_grant": (alias["generation"], "response_format", "json_object"),
        "reasoning_without_grant": (alias["generation"], "reasoning_effort", "low"),
        "verbosity_without_grant": (alias["generation"], "text_verbosity", "low"),
        "missing_output_cap": (alias["limits"], "max_output_tokens", None),
        "cache_without_retention": (data, "evidence_policy", {"cache_enabled": True}),
        "missing_auth": (data, "access", {}),
        "disabled_provider": (data["providers"]["router"], "enabled", False),
        "missing_credential": (data["providers"]["router"], "credential", None),
        "model_endpoint": (alias, "upstream_model", "http:untrusted"),
        "wrong_provider_prefix": (data["providers"]["router"], "kind", "openai"),
        "missing_budget": (alias, "budget_policy_refs", ["does-not-exist"]),
        "missing_policy": (alias, "operation_policy_ref", "does-not-exist"),
        "missing_pricing": (alias, "pricing", None),
        "no_run_cap": (alias, "budget_policy_refs", ["call"]),
        "override_unknown_alias": (data, "overrides", {"missing": "EXAMPLE_MODEL"}),
        "principal_unknown_alias": (data["access"]["principals"]["test"], "aliases", ["missing"]),
        "compat_unknown_alias": (
            data,
            "compatibility",
            {"dashboard_profiles": {"test": ["missing"]}},
        ),
        "fixture_unknown_alias": (
            data,
            "fake",
            {"fixture_root": "/unused", "responses": {"missing": {"default": ["reply.txt"]}}},
        ),
    }
    if scenario in changes:
        target, key, value = changes[scenario]
        target[key] = value
    elif scenario == "decision_output_cap":
        data["aliases"]["decision"]["limits"]["max_output_tokens"] = 1
    elif scenario == "nonchat_unknown_retry":
        data["aliases"]["response"]["attempt_policy"]["unknown_charge_policy"] = "bounded_exposure"
    elif scenario == "duplicate_handles":
        data["operation_policies"]["scope"]["allowed_x_handles"] = ["Public", "public"]
    else:
        alias["attempt_policy"]["max_attempts"] = 2
        fallback = {
            "provider_ref": "router",
            "upstream_model": "synthetic/fallback",
            "pricing": copy.deepcopy(alias["pricing"]),
            "capabilities": copy.deepcopy(alias["capabilities"]),
        }
        alias["attempt_policy"]["fallback_routes"] = [fallback]
        if scenario == "unknown_retry_fallback":
            alias["attempt_policy"]["unknown_charge_policy"] = "bounded_exposure"
        elif scenario == "fallback_missing_provider":
            fallback["provider_ref"] = "missing"
        elif scenario == "fallback_disabled":
            data["providers"]["disabled"] = {"kind": "openrouter", "enabled": False}
            fallback["provider_ref"] = "disabled"
        elif scenario == "fallback_endpoint":
            fallback["upstream_model"] = "http:untrusted"
        elif scenario == "fallback_wrong_provider":
            fallback["provider_ref"] = "direct"
        elif scenario == "fallback_free_paid_route":
            fallback["pricing"] = {
                "revision": "synthetic",
                "bound_source": "synthetic",
                "free_only": True,
            }
        elif scenario == "fallback_lost_capability":
            fallback["capabilities"] = ["plain_text"]
        else:
            raise AssertionError("Unimplemented regression scenario")
    return data


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_configuration_cannot_expand_runtime_or_accounting_authority(tmp_path, scenario):
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(configuration(tmp_path, scenario))


def test_declared_capability_preserving_fallback_keeps_independent_provider_route(tmp_path):
    data = configuration(tmp_path, "fallback_wrong_provider")
    fallback = data["aliases"]["summary"]["attempt_policy"]["fallback_routes"][0]
    fallback["upstream_model"] = "synthetic-fallback"
    validated = ProjectConfig.model_validate(data)
    assert validated.aliases["summary"].attempt_policy.fallback_routes[0].provider_ref == "direct"
    assert validated.aliases["summary"].attempt_policy.max_attempts == 2


def test_explicit_free_fallback_requires_the_actual_free_route(tmp_path):
    data = configuration(tmp_path, "fallback_free_paid_route")
    fallback = data["aliases"]["summary"]["attempt_policy"]["fallback_routes"][0]
    fallback["upstream_model"] = "openrouter/free"
    validated = ProjectConfig.model_validate(data)
    assert validated.aliases["summary"].attempt_policy.fallback_routes[0].pricing.free_only is True
