"""Project-owned credential variable names never become OS/SDK startup controls."""

import hashlib
import subprocess
import sys

import pytest
from test_config import private_file
from test_provider_process import prepared

from llm_gateway.config.models import CredentialReference, ProviderConfig
from llm_gateway.config.secrets import resolve_credential
from llm_gateway.providers.process import (
    WORKER_KEY,
    WORKER_KEY_FILE,
    OwnedProviderProcess,
    worker_environment,
    worker_provider,
)

pytestmark = pytest.mark.offline


@pytest.mark.parametrize(
    "name",
    [
        "LANGUAGE",
        "LC_ALL",
        "LOCALDOMAIN",
        "RES_OPTIONS",
        "GCONV_PATH",
        "GLIBC_TUNABLES",
        "LD_PRELOAD",
        "PYTHONPATH",
    ],
)
def test_even_historical_control_names_are_rebound_before_actual_child_start(
    tmp_path, monkeypatch, name
):
    # Legacy references remain readable for receipts; a direct owned-process
    # handoff must still be safe independently of current config validation.
    reference = CredentialReference.model_validate(
        {"env_var": name}, context={"legacy_snapshot": True}
    )
    provider = ProviderConfig(kind="openai", credential=reference)
    call, _ = prepared()
    original = subprocess.Popen
    script = """import os, sys, json, hashlib
from llm_gateway.config.models import ProviderConfig
from llm_gateway.config.secrets import resolve_credential
message = json.loads(sys.stdin.buffer.read())
provider = ProviderConfig.model_validate(message['provider'])
value = resolve_credential(provider.credential)
evidence = {
    'payload': {'names': sorted(os.environ),
                'digest': hashlib.sha256(value.encode()).hexdigest()},
    'usage': {}, 'measured_usd': None, 'actual_model': None,
    'actual_provider': None, 'generation_id': None, 'billing_anomaly': False,
}
print(json.dumps({'evidence': evidence}))
"""

    def launch(args, **kwargs):
        assert name not in kwargs["env"] and kwargs["env"]["LANG"] == "C.UTF-8"
        return original([sys.executable, "-I", "-c", script], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    result = OwnedProviderProcess(tmp_path, environment={name: "synthetic-credential"}).invoke(
        call, provider
    )
    assert result.payload["digest"] == hashlib.sha256(b"synthetic-credential").hexdigest()
    assert name not in result.payload["names"] and WORKER_KEY in result.payload["names"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("choice", ["env", "file_env", "file_path", "absent"])
def test_rebinding_preserves_explicit_file_precedence_without_mutating_project_reference(
    tmp_path, choice
):
    path = private_file(tmp_path, "key", "synthetic-file-value")
    reference = CredentialReference(
        env_var="PROJECT_KEY",
        file_env_var="PROJECT_KEY_FILE",
        file_path=str(path) if choice == "file_path" else None,
    )
    provider = ProviderConfig(kind="openrouter", credential=reference)
    source = {}
    if choice != "absent":
        source["PROJECT_KEY"] = "synthetic-env-value"
    if choice == "file_env":
        source["PROJECT_KEY_FILE"] = str(path)
    rebound = worker_provider(provider)
    environment = worker_environment(provider, private_home=str(tmp_path), environment=source)
    assert provider.credential.env_var == "PROJECT_KEY"
    assert (
        rebound.credential.env_var == WORKER_KEY
        and rebound.credential.file_env_var == WORKER_KEY_FILE
    )
    assert "PROJECT_KEY" not in environment and "PROJECT_KEY_FILE" not in environment
    if choice == "absent":
        with pytest.raises(ValueError, match="credential_unavailable"):
            resolve_credential(rebound.credential, environment)
    else:
        assert resolve_credential(rebound.credential, environment) == (
            "synthetic-env-value" if choice == "env" else "synthetic-file-value"
        )


def test_no_credential_reference_does_not_add_handoff_authority(tmp_path):
    provider = ProviderConfig(kind="openai")
    assert worker_provider(provider) is provider
    environment = worker_environment(
        provider,
        private_home=str(tmp_path),
        environment={WORKER_KEY: "unrelated", "LANGUAGE": "unrelated"},
    )
    assert WORKER_KEY not in environment and "LANGUAGE" not in environment
