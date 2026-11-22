"""T121: executable ownership and graceful lifecycle, without inference keys."""

import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from test_config import synthetic_config, write_config
from test_engine import configured
from test_ledger import request

from llm_gateway.client import GatewayClient, GatewayTransportError
from llm_gateway.service.engine import EngineError
from llm_gateway.storage import StateStore, StoreError
from llm_gateway.storage.lease import StateLease

pytestmark = pytest.mark.offline


def command(path, root, *args):
    return [
        sys.executable,
        "-I",
        "-m",
        "llm_gateway.service.cli",
        "serve",
        "--project-config",
        str(path),
        "--state-dir",
        str(root),
        *map(str, args),
    ]


def test_lease_excludes_another_owner_and_releases_after_error(tmp_path):
    with pytest.raises(RuntimeError):
        with StateLease(tmp_path / "state"):
            with pytest.raises(StoreError, match="already_owned"):
                StateLease(tmp_path / "state")
            raise RuntimeError()
    with StateLease(tmp_path / "state") as lease:
        assert lease.root.stat().st_mode & 0o077 == 0


def test_lease_rejects_public_lock_and_symlink_root(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    (root / "service.lock").touch(mode=0o644)
    with pytest.raises(StoreError, match="not_private"):
        StateLease(root)
    (tmp_path / "link").symlink_to(root)
    with pytest.raises(StoreError, match="symlink"):
        StateLease(tmp_path / "link")


def test_tcp_requires_bearer_configuration_before_listen(tmp_path):
    path = write_config(tmp_path, synthetic_config())
    child = subprocess.run(
        command(path, tmp_path / "state", "--port", "0"), capture_output=True, text=True, timeout=10
    )
    assert child.returncode == 2
    assert json.loads(child.stderr)["code"] == "project_requires_bearer_or_dedicated_owner_socket"


def test_installed_unix_server_fake_inference_shutdown_and_ledger_survive():
    # macOS has a short Unix path limit; a short private /tmp root is deliberate.
    with tempfile.TemporaryDirectory(prefix="gw-", dir="/tmp") as directory:
        root = Path(directory)
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.bind(str(root / "probe"))
        except PermissionError:
            pytest.skip("sandbox denies socket binding; run the local socket check with permission")
        finally:
            probe.close()
        (root / "probe").unlink()
        fixtures = root / "fixtures"
        fixtures.mkdir()
        (fixtures / "reply.txt").write_text("hello from a fixture")
        data = synthetic_config()
        data["access"] = {"owner_only_ipc": True}
        data["fake"] = {
            "fixture_root": str(fixtures),
            "responses": {"summary": {"default": ["reply.txt"]}},
        }
        path = write_config(root, data)
        uds, state = root / "gateway.sock", root / "state"
        args = command(path, state, "--unix-socket", uds)
        child = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={"PATH": os.environ["PATH"]},
            cwd=root,
        )
        try:
            assert select.select([child.stdout], [], [], 10)[0], "server startup timed out"
            assert json.loads(child.stdout.readline())["status"] == "listening"
            duplicate = subprocess.run(args, capture_output=True, text=True, timeout=10)
            assert duplicate.returncode == 2
            assert json.loads(duplicate.stderr)["code"] == "state_service_already_owned"
            with GatewayClient("http://localhost", unix_socket=uds, timeout_seconds=5) as client:
                for _ in range(100):
                    try:
                        assert client.ready().status == "ready"
                        break
                    except GatewayTransportError:
                        time.sleep(0.02)
                else:
                    pytest.fail("server readiness timed out")
                result = client.infer(request())
                assert result.content == "hello from a fixture"
                assert result.receipts[0].evidence_status == "simulated"
            child.terminate()
            assert child.wait(timeout=10) == 0
            assert not uds.exists()
            with StateLease(state):
                store = StateStore(state)
                with store.connection() as db:
                    assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


def test_shutdown_fences_calls_and_drains_even_before_registration(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    engine, authority, _ = configured(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = engine._run

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "_run", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(engine.run, authority, request())
        assert entered.wait(1)
        engine.begin_shutdown()
        draining = pool.submit(engine.shutdown)
        assert not draining.done()
        with pytest.raises(EngineError, match="shutting_down"):
            engine.run(authority, request("two"))
        release.set()
        with pytest.raises(EngineError, match="cancelled"):
            running.result(timeout=3)
        draining.result(timeout=3)
    assert not engine.provider.calls
