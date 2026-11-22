"""Implicit server-side features must not escape the declared monetary bound."""

import copy

import pytest
from pydantic import ValidationError
from test_config import synthetic_config, write_config
from test_config_boundaries import configuration
from test_provider_runtime import alias

from llm_gateway.config import ConfigError, load_project_config
from llm_gateway.config.models import ProjectConfig, ProviderConfig
from llm_gateway.contracts.models import ChatInput, Message
from llm_gateway.providers.preparation import PreparationError, prepare

pytestmark = pytest.mark.offline


@pytest.mark.parametrize(
    "model",
    ["synthetic/model:online", "synthetic/model:free:online", "synthetic/model:ONLINE:free"],
)
def test_implicit_web_variants_cannot_be_primary_fallback_or_override(tmp_path, model):
    primary = synthetic_config(model=model)
    with pytest.raises(ValidationError, match="implicit_web_search_variant_forbidden"):
        ProjectConfig.model_validate(primary)
    fallback = configuration(tmp_path, "fallback_wrong_provider")
    fallback["aliases"]["summary"]["attempt_policy"]["fallback_routes"][0].update(
        provider_ref="router", upstream_model=model
    )
    with pytest.raises(ValidationError, match="implicit_web_search_variant_forbidden"):
        ProjectConfig.model_validate(fallback)
    overridden = synthetic_config()
    overridden["overrides"] = {"summary": "EXAMPLE_MODEL"}
    with pytest.raises(ConfigError, match="schema_invalid"):
        load_project_config(
            write_config(tmp_path, overridden), environment={"EXAMPLE_MODEL": model}
        )


@pytest.mark.parametrize("model", ["synthetic/model:online", "synthetic/model:ONLINE:free"])
def test_historical_snapshot_cannot_dispatch_implicit_web_variant(model):
    data = synthetic_config(model=model)
    historical = ProjectConfig.model_validate(data, context={"legacy_snapshot": True})
    with pytest.raises(PreparationError, match="implicit_web_search_variant_forbidden"):
        prepare(
            historical.aliases["summary"],
            historical.providers["router"],
            ChatInput(messages=[Message(role="user", content="public synthetic fixture")]),
        )


@pytest.mark.parametrize(
    "kind,model",
    [
        ("openrouter", "synthetic/model-online"),
        ("openrouter", "synthetic/model:free"),
        ("openai", "ft:synthetic:online"),
    ],
)
def test_only_openrouter_colon_variant_has_this_semantic(kind, model):
    data = copy.deepcopy(synthetic_config(model=model))
    data["providers"]["router"]["kind"] = kind
    ProjectConfig.model_validate(data)
    prepared = prepare(
        alias(model=model),
        ProviderConfig(kind=kind),
        ChatInput(messages=[Message(role="user", content="public synthetic fixture")]),
    )
    assert prepared.body["model"] == model
