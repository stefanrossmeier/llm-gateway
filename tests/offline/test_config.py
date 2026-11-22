"""ARCH-10/MODEL-14: owning projects supply all aliases and config, without fallback."""

import json

import pytest
import yaml

from llm_gateway.config import ConfigError, load_project_config
from llm_gateway.config.loader import parse_yaml
from llm_gateway.config.models import CredentialReference
from llm_gateway.config.secrets import SecretUnavailable, resolve_credential
from llm_gateway.service.cli import main

pytestmark = pytest.mark.offline


def synthetic_config(project="project-a", model="fixture-model"):
    return dict(
        schema_version=1,
        project_id=project,
        mode="fake",
        providers={"router": {"kind": "openrouter"}},
        aliases={
            "summary": {
                "provider_ref": "router",
                "upstream_model": model,
                "operation": "chat",
                "capabilities": ["plain_text"],
                "limits": {
                    "max_input_bytes": 1024,
                    "max_input_tokens": 1024,
                    "max_output_tokens": 64,
                    "attempt_timeout_seconds": 1,
                    "request_timeout_seconds": 2,
                    "max_concurrency": 1,
                },
            }
        },
    )


def write_config(tmp_path, data):
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def private_file(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(value)
    path.chmod(0o600)
    return path


def test_no_config_discovery_or_bundled_aliases(tmp_path):
    with pytest.raises(ConfigError, match="file_unavailable"):
        load_project_config(tmp_path / "missing.yaml")
    data = synthetic_config()
    data["aliases"] = {}
    with pytest.raises(ConfigError, match="schema_invalid"):
        load_project_config(write_config(tmp_path, data))


@pytest.mark.parametrize(
    "body",
    [
        b"project_id: first\nproject_id: second",
        b"x: &anchor hi\ny: *anchor",
        b"!!python/object/apply:os.system ['false']",
        b"<<: {x: 1}\nx: 2",
        b"[1,2,3]",
        b"x: " + b"[" * 65 + b"1" + b"]" * 65,
    ],
)
def test_unsafe_or_unbounded_yaml_is_rejected(body):
    with pytest.raises(ConfigError):
        parse_yaml(body)


@pytest.mark.parametrize(
    "change",
    [
        {"api_key": "must-not-leak"},
        {"schema_version": 2},
        {"timezone": "Invalid/Place"},
        {"project_id": "../../escape"},
        {"aliases": {}},
    ],
)
def test_invalid_config_is_safe_and_has_no_raw_values(tmp_path, change):
    data = synthetic_config() | change
    with pytest.raises(ConfigError) as caught:
        load_project_config(write_config(tmp_path, data))
    assert "must-not-leak" not in str(caught.value)
    assert caught.value.code == "configuration_schema_invalid"


def test_same_alias_is_project_owned_and_snapshot_is_immutable(tmp_path):
    a = load_project_config(write_config(tmp_path, synthetic_config()))
    b = load_project_config(write_config(tmp_path, synthetic_config("project-b", "other-model")))
    assert a.config.aliases["summary"].upstream_model == "fixture-model"
    assert b.config.aliases["summary"].upstream_model == "other-model"
    assert a.digest != b.digest
    changed = a.config
    changed.aliases.clear()
    assert "summary" in a.config.aliases
    with pytest.raises(ConfigError, match="project_mismatch"):
        load_project_config(
            write_config(tmp_path, synthetic_config()), expected_project_id="project-b"
        )


def test_snapshot_hash_is_stable_and_overrides_are_explicit(tmp_path):
    data = synthetic_config()
    path = write_config(tmp_path, data)
    first = load_project_config(path, environment={"UNDECLARED_MODEL": "ignored"})
    assert first.digest == load_project_config(path).digest
    data["overrides"] = {"summary": "EXAMPLE_SUMMARY_MODEL"}
    second = load_project_config(
        write_config(tmp_path, data), environment={"EXAMPLE_SUMMARY_MODEL": "changed-model"}
    )
    assert second.config.aliases["summary"].upstream_model == "changed-model"
    assert second.digest != first.digest
    data["overrides"] = {"summary": "OPENAI_API_KEY"}
    with pytest.raises(ConfigError, match="secret_reference_forbidden"):
        load_project_config(write_config(tmp_path, data), environment={"OPENAI_API_KEY": "secret"})


def test_file_env_precedence_and_invalid_explicit_file_never_falls_back(tmp_path):
    selected = private_file(tmp_path, "selected", "synthetic-selected")
    declared = private_file(tmp_path, "declared", "synthetic-declared")
    reference = CredentialReference(
        env_var="EXAMPLE_KEY", file_env_var="EXAMPLE_KEY_FILE", file_path=str(declared)
    )
    assert (
        resolve_credential(
            reference, {"EXAMPLE_KEY_FILE": str(selected), "EXAMPLE_KEY": "synthetic-env"}
        )
        == "synthetic-selected"
    )
    assert resolve_credential(reference, {"EXAMPLE_KEY": "synthetic-env"}) == "synthetic-declared"
    selected.unlink()
    with pytest.raises(SecretUnavailable):
        resolve_credential(
            reference, {"EXAMPLE_KEY_FILE": str(selected), "EXAMPLE_KEY": "fallback"}
        )
    declared.chmod(0o644)
    with pytest.raises(SecretUnavailable):
        resolve_credential(reference, {"EXAMPLE_KEY": "fallback"})


def test_fake_mode_does_not_read_upstream_keys(tmp_path, monkeypatch):
    data = synthetic_config()
    data["providers"]["router"]["credential"] = {"file_path": "/not/a/real/key"}
    monkeypatch.setattr(
        "llm_gateway.config.loader.resolve_credential",
        lambda *_args: pytest.fail("fake mode read a key"),
    )
    assert load_project_config(write_config(tmp_path, data)).config.mode == "fake"


def free_config():
    data = synthetic_config()
    data["mode"] = "live"
    data["access"] = {"owner_only_ipc": True}
    data["providers"]["router"]["credential"] = {"env_var": "EXAMPLE_ROUTER_KEY"}
    profile = data["aliases"]["summary"]
    profile["upstream_model"] = "openrouter/free"
    profile["pricing"] = {
        "free_only": True,
        "revision": "public-free-1",
        "bound_source": "synthetic free-policy evidence",
    }
    return data


def test_live_routes_validate_only_declared_keys_and_never_hash_values(tmp_path):
    data = free_config()
    data["providers"]["direct"] = {"kind": "openai", "enabled": False}
    path = write_config(tmp_path, data)
    a = load_project_config(path, environment={"EXAMPLE_ROUTER_KEY": "synthetic-first"})
    b = load_project_config(path, environment={"EXAMPLE_ROUTER_KEY": "synthetic-rotated"})
    assert a.digest == b.digest
    assert "synthetic-first" not in a.payload
    with pytest.raises(ConfigError, match="credential_unavailable"):
        load_project_config(path, environment={})


@pytest.mark.parametrize(
    "change",
    [
        {"upstream_model": "paid/model"},
        {"provider_ref": "missing"},
        {"capabilities": ["native_x_search"]},
        {"upstream_model": "https://unapproved/model"},
        {
            "pricing": {
                "free_only": True,
                "revision": "x",
                "bound_source": "x",
                "tool_fees_usd": {"search": "0.1"},
            }
        },
    ],
)
def test_free_policy_cannot_become_paid_or_hosted_search(tmp_path, change):
    data = free_config()
    data["aliases"]["summary"].update(change)
    with pytest.raises(ConfigError):
        load_project_config(write_config(tmp_path, data), check_credentials=False)


def test_paid_policy_needs_call_and_run_caps(tmp_path):
    data = free_config()
    data["aliases"]["summary"]["upstream_model"] = "public/model"
    data["aliases"]["summary"]["pricing"]["free_only"] = False
    with pytest.raises(ConfigError):
        load_project_config(write_config(tmp_path, data), check_credentials=False)
    data["budgets"] = {
        "call": {"scope": "project", "period": "call", "cap_usd": "0.01"},
        "run": {"scope": "run", "period": "run", "cap_usd": "0.1"},
    }
    data["aliases"]["summary"]["budget_policy_refs"] = ["call", "run"]
    assert (
        load_project_config(write_config(tmp_path, data), check_credentials=False)
        .config.budgets["run"]
        .cap_usd
        > 0
    )


def test_captured_fixtures_cannot_change_an_admitted_snapshot(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    response = fixtures / "one.txt"
    response.write_text("original")
    data = synthetic_config()
    data["fake"] = {
        "fixture_root": str(fixtures),
        "responses": {"summary": {"default": ["one.txt"]}},
    }
    path = write_config(tmp_path, data)
    first = load_project_config(path)
    response.write_text("changed")
    assert first.fixtures["one.txt"]["content"] == "original"
    assert load_project_config(path).digest != first.digest


@pytest.mark.parametrize("filename", ["../outside.txt", "/outside.txt", "", "https://host/file"])
def test_fixture_paths_cannot_escape_the_mount(tmp_path, filename):
    data = synthetic_config()
    data["fake"] = {
        "fixture_root": str(tmp_path),
        "responses": {"summary": {"default": [filename]}},
    }
    with pytest.raises(ConfigError):
        load_project_config(write_config(tmp_path, data))


def test_fixture_symlink_escape_is_rejected(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (fixtures / "link").symlink_to(outside)
    data = synthetic_config()
    data["fake"] = {"fixture_root": str(fixtures), "responses": {"summary": {"default": ["link"]}}}
    with pytest.raises(ConfigError, match="fixture_path_escape"):
        load_project_config(write_config(tmp_path, data))


def test_cli_validation_is_safe_and_never_rewrites_project_file(tmp_path, capsys):
    path = write_config(tmp_path, synthetic_config())
    original = path.read_bytes()
    assert main(["config", "validate", "--project-config", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["project_id"] == "project-a" and len(result["config_digest"]) == 64
    assert path.read_bytes() == original
    data = synthetic_config()
    data["aliases"]["summary"]["api_key"] = "never-display-this"
    write_config(tmp_path, data)
    assert main(["config", "validate", "--project-config", str(path)]) == 2
    assert "never-display-this" not in capsys.readouterr().err


def test_installed_service_cli_outside_checkout(tmp_path):
    import subprocess
    import sys

    path = write_config(tmp_path, synthetic_config())
    # -I removes pytest's source path: this must exercise the installed wheel.
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "llm_gateway.service.cli",
            "config",
            "validate",
            "--project-config",
            str(path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["project_id"] == "project-a"


def test_dynamic_run_authority_needs_calendar_budget_and_no_fixed_binding(tmp_path):
    from test_engine import configured

    def mutate(data):
        data["access"] = {
            "principals": {
                "runner": {
                    "token_file": "/private/unused",
                    "aliases": ["summary"],
                    "operations": ["chat"],
                    "permissions": ["infer"],
                    "allow_client_run_ids": True,
                }
            }
        }

    with pytest.raises(ValueError, match="configuration_schema_invalid"):
        configured(tmp_path, mutate=mutate)

    def calendar(data):
        mutate(data)
        data["budgets"]["daily"] = {"scope": "project", "period": "day", "cap_usd": "2"}
        data["aliases"]["summary"]["budget_policy_refs"].append("daily")

    configured(tmp_path, mutate=calendar)

    def conflict(data):
        calendar(data)
        data["access"]["principals"]["runner"]["run_id"] = "fixed"

    with pytest.raises(ValueError, match="configuration_schema_invalid"):
        configured(tmp_path, mutate=conflict)
