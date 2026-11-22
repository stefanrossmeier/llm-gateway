import json
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from test_provider_runtime import alias

from llm_gateway.config.models import ProviderConfig
from llm_gateway.contracts import ChatInput, Message
from llm_gateway.providers.preparation import prepare
from llm_gateway.providers.process import WORKER_KEY, OwnedProviderProcess, worker_environment
from llm_gateway.providers.runtime import ProviderFailure

pytestmark = pytest.mark.offline


def prepared():
    provider = ProviderConfig(kind="openai", credential={"env_var": "ONLY_KEY"})
    return replace(
        prepare(alias(), provider, ChatInput(messages=[Message(role="user", content="public")])),
        timeout_seconds=3,
    ), provider


def test_worker_environment_contains_only_declared_secrets(tmp_path):
    _, provider = prepared()
    env = worker_environment(
        provider,
        private_home=str(tmp_path),
        environment={
            "ONLY_KEY": "public-synthetic",
            "AWS_SECRET_ACCESS_KEY": "unrelated",
            "GITHUB_TOKEN": "unrelated",
            "HTTPS_PROXY": "https://invalid",
            "OPENAI_API_BASE": "https://evil.invalid",
            "PYTHONPATH": "/untrusted",
        },
    )
    assert env[WORKER_KEY] == "public-synthetic"
    assert set(env) == {
        WORKER_KEY,
        "HOME",
        "TMPDIR",
        "PYTHONNOUSERSITE",
        "LANG",
        "LITELLM_MODE",
        "LITELLM_LOCAL_MODEL_COST_MAP",
        "HF_HUB_OFFLINE",
        "DO_NOT_TRACK",
    }


def test_actual_worker_rejects_missing_credential_without_network_or_secret_echo(tmp_path):
    call, provider = prepared()
    process = OwnedProviderProcess(tmp_path, environment={"UNRELATED_KEY": "must-not-leak"})
    with pytest.raises(ProviderFailure) as caught:
        process.invoke(call, provider)
    assert not caught.value.dispatched
    assert "must-not-leak" not in str(caught.value)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("cancelled", [False, True])
def test_deadline_and_cancellation_kill_owned_group(tmp_path, monkeypatch, cancelled):
    import llm_gateway.isolation.process as module

    popen = subprocess.Popen
    children = []

    def launch(args, **kwargs):
        assert kwargs["start_new_session"] is True and kwargs["cwd"] != str(Path.cwd())
        assert WORKER_KEY in kwargs["env"] and "GITHUB_TOKEN" not in kwargs["env"]
        process = popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    event = threading.Event()
    if cancelled:
        event.set()
    call, provider = prepared()
    with pytest.raises(ProviderFailure) as caught:
        OwnedProviderProcess(tmp_path, environment={"ONLY_KEY": "synthetic"}).invoke(
            call, provider, cancel=event, timeout=0.1
        )
    assert caught.value.code == (
        "provider_cancelled" if cancelled else "provider_deadline_exceeded"
    )
    if cancelled:
        assert not children
        assert not caught.value.dispatched
    else:
        assert children[0].poll() is not None
    assert not list(tmp_path.iterdir())


def test_protocol_keeps_null_usage_and_exact_measured_cost(tmp_path, monkeypatch):
    from decimal import Decimal

    import llm_gateway.isolation.process as module

    popen = subprocess.Popen
    response = {
        "evidence": {
            "payload": {"choices": []},
            "usage": {},
            "measured_usd": "0.000000000000000001",
            "actual_model": None,
            "actual_provider": None,
            "generation_id": None,
            "billing_anomaly": False,
        }
    }

    def launch(args, **kwargs):
        return popen(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; sys.stdin.buffer.read(); sys.stdout.write("
                + repr(json.dumps(response))
                + ")",
            ],
            **kwargs,
        )

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    call, provider = prepared()
    result = OwnedProviderProcess(tmp_path).invoke(call, provider)
    assert result.usage.input_tokens is None
    assert result.measured_usd == Decimal("0.000000000000000001")


def test_empty_worker_evidence_keeps_dispatch_uncertainty(tmp_path, monkeypatch):
    import llm_gateway.isolation.process as module

    original = subprocess.Popen

    def launch(args, **kwargs):
        return original(
            [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read(); print('{}')"],
            **kwargs,
        )

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    call, provider = prepared()
    with pytest.raises(ProviderFailure) as error:
        OwnedProviderProcess(tmp_path).invoke(call, provider)
    assert error.value.code == "provider_process_lost" and error.value.dispatched
    assert not list(tmp_path.iterdir())
