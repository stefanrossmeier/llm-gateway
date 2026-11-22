"""Offline is the default: provider credentials and internet sockets are unavailable."""

import os
import socket
import subprocess
import sysconfig
from pathlib import Path

import pytest

# Set before importing LiteLLM: do not auto-load .env or download model/tokenizer maps.
os.environ["LITELLM_MODE"] = "PRODUCTION"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["DO_NOT_TRACK"] = "1"


@pytest.fixture(autouse=True)
def inference_boundary(request, monkeypatch):
    if request.node.get_closest_marker("live_paid"):
        if not request.config.getoption("--run-live-paid") or not request.config.getoption(
            "--live-paid-approval"
        ):
            pytest.skip("Paid execution requires final owner approval, private record and flag")
        return
    if request.node.get_closest_marker("live_free"):
        if os.environ.get("LLM_GATEWAY_ENABLE_FREE_TESTS") != "1" or not request.config.getoption(
            "--run-live-free"
        ):
            pytest.skip("Explicit free-test enablement required")
        return
    for key in list(os.environ):
        if key.endswith(("API_KEY", "AUTH_TOKEN")) or key in {"GITHUB_TOKEN", "GH_TOKEN"}:
            monkeypatch.delenv(key)
    original_connect = socket.socket.connect

    def connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            raise AssertionError("offline test attempted a network connection")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture(autouse=True)
def owned_process_coverage(request, monkeypatch):
    """Explicit test instrumentation, never a production runtime/environment hook."""
    if any(request.node.get_closest_marker(name) for name in ("live_free", "live_paid")):
        return
    plugin = request.config.pluginmanager.getplugin("_cov")
    if plugin is None or plugin.cov_controller is None:
        return
    root = Path(__file__).resolve().parents[1]
    data_file = str(Path(plugin.cov_controller.cov.config.data_file).resolve())
    source = root / "src" / "llm_gateway"
    installed = Path(sysconfig.get_paths()["purelib"]) / "llm_gateway"
    original = subprocess.Popen

    def launch(args, **kwargs):
        if isinstance(args, list) and len(args) >= 3 and args[1] == "-I":
            candidate = Path(args[2]).resolve()
            worker = (
                len(args) == 3
                and candidate.name in {"worker.py", "validation_worker.py"}
                and any(candidate.is_relative_to(path) for path in (source, installed))
            )
            cli = args[2:4] in (
                ["-m", "llm_gateway.service.cli"],
                ["-m", "llm_gateway.testing.run"],
            )
            if worker or cli:
                args = [
                    args[0],
                    "-I",
                    "-m",
                    "coverage",
                    "run",
                    "--parallel-mode",
                    "--branch",
                    "--source=" + str(source) + "," + str(installed),
                    "--rcfile=" + str(root / "pyproject.toml"),
                    "--data-file=" + data_file,
                    *args[2:],
                ]
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)


def pytest_addoption(parser):
    group = parser.getgroup("gateway live acceptance")
    group.addoption("--run-live-free", action="store_true", default=False)
    group.addoption("--run-live-paid", action="store_true", default=False)
    group.addoption("--live-paid-approval")
    group.addoption("--live-plan")
    group.addoption("--live-project-config")
    group.addoption("--live-gateway-origin")
    group.addoption("--live-gateway-token-file")
    group.addoption("--live-report")
