"""File-defined model routing preserves destination, credential and snapshot boundaries."""

import json
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError
from test_config import synthetic_config, write_config
from test_provider_runtime import alias, payload

from llm_gateway.config import ConfigError, load_project_config
from llm_gateway.config.models import ProjectConfig, ProviderConfig
from llm_gateway.contracts import ChatInput, Message
from llm_gateway.providers.preparation import PreparationError, prepare
from llm_gateway.providers.runtime import ProviderFailure, ProviderRuntime
from llm_gateway.service.egress_proxy import destination

pytestmark = pytest.mark.offline


def generic(**changes):
    return (
        dict(
            adapter="openai_chat",
            base_url="https://inference.example/api/v1",
            authentication={"type": "none"},
            free_models=["example/free"],
        )
        | changes
    )


def file_project(tmp_path):
    data = synthetic_config(model="example/free")
    data["providers"] = {"router": {"definition": "providers/custom.yaml"}}
    profile = data["aliases"]["summary"]
    profile["pricing"] = {
        "revision": "fixture-free",
        "bound_source": "reviewed fixture",
        "free_only": True,
    }
    data["aliases"] = {"summary": {"definition": "models/summary.yaml"}}
    (tmp_path / "providers").mkdir()
    (tmp_path / "models").mkdir()
    (tmp_path / "providers/custom.yaml").write_text(yaml.safe_dump(generic()))
    (tmp_path / "models/summary.yaml").write_text(yaml.safe_dump(profile))
    return write_config(tmp_path, data)


def test_definitions_are_resolved_and_captured_in_snapshot(tmp_path):
    path = file_project(tmp_path)
    first = load_project_config(path)
    assert first.config.providers["router"].base_url == "https://inference.example/api/v1"
    assert first.config.aliases["summary"].provider_ref == "router"
    assert "definition" not in first.payload
    provider = tmp_path / "providers/custom.yaml"
    provider.write_text(yaml.safe_dump(generic(base_url="https://other.example/v1")))
    second = load_project_config(path)
    provider.unlink()
    assert first.digest != second.digest
    assert first.config.providers["router"].base_url == "https://inference.example/api/v1"
    assert second.config.providers["router"].base_url == "https://other.example/v1"


def test_bundled_provider_matches_legacy_inline_configuration(tmp_path):
    data = synthetic_config()
    inline = load_project_config(write_config(tmp_path, data))
    data["providers"]["router"] = {"definition": "builtin:openrouter"}
    bundled = load_project_config(write_config(tmp_path, data))
    assert inline.digest == bundled.digest
    assert ProviderConfig(adapter="openai").base_url == "https://api.openai.com/v1"


@pytest.mark.parametrize(
    "reference",
    ["../escape.yaml", "/tmp/escape.yaml", "https://evil.example/p", "a\\b", "", "builtin:missing"],
)
def test_definition_rejects_escaping_remote_and_unknown_sources(tmp_path, reference):
    data = synthetic_config()
    data["providers"]["router"] = {"definition": reference}
    with pytest.raises(ConfigError, match="definition"):
        load_project_config(write_config(tmp_path, data))


@pytest.mark.parametrize(
    "kind",
    [
        "leaf_link",
        "parent_link",
        "directory",
        "oversized",
        "recursive",
        "duplicate",
        "override",
        "bad_reference",
    ],
)
def test_definition_files_are_bounded_unambiguous_and_no_follow(tmp_path, kind):
    path = file_project(tmp_path)
    provider = tmp_path / "providers/custom.yaml"
    if kind == "leaf_link":
        original = tmp_path / "original.yaml"
        provider.rename(original)
        provider.symlink_to(original)
    elif kind == "parent_link":
        (tmp_path / "providers").rename(tmp_path / "original")
        (tmp_path / "providers").symlink_to(tmp_path / "original", target_is_directory=True)
    elif kind == "directory":
        provider.unlink()
        provider.mkdir()
    elif kind == "oversized":
        provider.write_bytes(b"x" * 1048577)
    elif kind == "recursive":
        provider.write_text("definition: another.yaml\n")
    elif kind == "duplicate":
        provider.write_text("adapter: openai_chat\nadapter: openai\n")
    else:
        data = yaml.safe_load(path.read_text())
        if kind == "override":
            data["providers"]["router"]["base_url"] = "https://override.example"
        else:
            data["providers"]["router"]["definition"] = 1
        path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_project_config(path)


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example/v1",
        "https://127.0.0.1/v1",
        "https://localhost/v1",
        "https://user:secret@public.example/v1",
        "https://public.example:444/v1",
        "https://public.example/v1?",
        "https://public.example/v1#",
        "https://public.example/a/../b",
        "https://public.example/%2e",
        "https://public.example//v1",
        "https://public.example/v1\n",
    ],
)
def test_provider_endpoint_is_explicit_https_without_overrides(url):
    with pytest.raises(ValidationError):
        ProviderConfig.model_validate(generic(base_url=url))


@pytest.mark.parametrize(
    "change",
    [
        dict(operations=["responses"]),
        dict(credential={"env_var": "SYNTHETIC_KEY"}),
        dict(free_models=["https://evil.example"]),
        dict(free_model_suffixes=["*"]),
    ],
)
def test_provider_auth_operations_and_free_routes_are_explicit(change):
    with pytest.raises(ValidationError):
        ProviderConfig.model_validate(generic(**change))


def test_live_anonymous_route_needs_explicit_free_model_evidence(tmp_path):
    path = file_project(tmp_path)
    data = yaml.safe_load(path.read_text())
    data.update(mode="live", access={"owner_only_ipc": True})
    path.write_text(yaml.safe_dump(data))
    assert load_project_config(path).config.providers["router"].credential is None
    profile = tmp_path / "models/summary.yaml"
    raw = yaml.safe_load(profile.read_text())
    raw["upstream_model"] = "example/paid"
    profile.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="schema_invalid"):
        load_project_config(path)


@pytest.mark.parametrize("authenticated", [False, True])
def test_generic_chat_uses_configured_endpoint_and_exact_auth_once(authenticated):
    raw = generic(authentication={"type": "bearer" if authenticated else "none"})
    if authenticated:
        raw["credential"] = {"env_var": "SYNTHETIC_KEY"}
    provider = ProviderConfig.model_validate(raw)
    calls = []

    def send(request):
        calls.append(request)
        assert str(request.url) == "https://inference.example/api/v1/chat/completions"
        assert request.headers.get("authorization") == (
            "Bearer synthetic-value" if authenticated else None
        )
        assert json.loads(request.content)["model"] == "example/free"
        return httpx.Response(200, json=payload("chat"))

    data = ChatInput(messages=[Message(role="user", content="public")])
    result = ProviderRuntime(
        transport_factory=lambda: httpx.MockTransport(send),
        environment={"SYNTHETIC_KEY": "synthetic-value"},
    ).invoke(prepare(alias(model="example/free"), provider, data), provider)
    assert result.payload["choices"][0]["message"]["content"] == "ok"
    assert len(calls) == 1


@pytest.mark.parametrize("status", [302, 401, 429, 503])
def test_generic_chat_never_follows_redirects_or_retries(status):
    provider = ProviderConfig.model_validate(generic())
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(
            status, headers={"Location": "https://evil.example"}, json={"error": "synthetic"}
        )

    data = ChatInput(messages=[Message(role="user", content="public")])
    with pytest.raises(ProviderFailure):
        ProviderRuntime(transport_factory=lambda: httpx.MockTransport(send)).invoke(
            prepare(alias(), provider, data), provider
        )
    assert len(calls) == 1


def test_egress_accepts_only_hosts_from_selected_provider_configuration():
    hosts = {"inference.example"}
    assert (
        destination(b"CONNECT inference.example:443 HTTP/1.1\r\n\r\n", hosts) == "inference.example"
    )
    with pytest.raises(ValueError):
        destination(b"CONNECT openrouter.ai:443 HTTP/1.1\r\n\r\n", hosts)


def test_generic_adapter_cannot_prepare_unsupported_operation():
    provider = ProviderConfig.model_validate(generic())
    from llm_gateway.contracts import ResponsesInput

    with pytest.raises(PreparationError, match="operation_route_mismatch"):
        prepare(alias("responses"), provider, ResponsesInput(input="public"))


def test_kilo_example_loads_without_provider_credentials():
    path = Path(__file__).resolve().parents[2] / "examples/free-chat/gateway/project.yaml"
    snapshot = load_project_config(path, check_credentials=False)
    assert snapshot.config.providers["kilo"].kind == "openai_chat"
    assert snapshot.config.aliases["kilo-auto-free"].upstream_model == "kilo-auto/free"


def test_missing_bearer_credentials_still_fail_closed():
    data = synthetic_config()
    data.update(mode="live", access={"owner_only_ipc": True})
    with pytest.raises(ValidationError, match="credential reference"):
        ProjectConfig.model_validate(data)
