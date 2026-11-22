"""Listener/CLI failures preserve state and another owner's socket identity."""

import builtins
import json
import signal
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_config import write_config

from llm_gateway.config import load_project_config
from llm_gateway.service import cli, server
from llm_gateway.storage import StateStore, StoreError
from llm_gateway.storage.lease import StateLease

pytestmark = pytest.mark.offline
pytest_plugins = ["test_api"]


@pytest.mark.parametrize("path", [Path("relative.sock"), Path("/" + "x" * 101)])
def test_unix_listener_rejects_relative_or_oversized_path(path):
    with pytest.raises(StoreError, match="short_absolute"):
        server.bind_unix(path)


@pytest.mark.parametrize(
    "kind", ["occupied", "symlink", "bind_failure", "listen_failure", "replacement"]
)
def test_socket_failure_only_removes_the_inode_this_bind_created(monkeypatch, kind):
    with tempfile.TemporaryDirectory(prefix="lg-bind-", dir="/tmp") as directory:
        path = Path(directory).resolve() / "gateway.sock"
        closed = []
        if kind == "occupied":
            path.write_text("another owner")
        elif kind == "symlink":
            path.symlink_to(path.parent / "elsewhere")

        class Socket:
            def bind(self, address):
                if kind == "bind_failure":
                    path.write_text("concurrent owner")
                    raise OSError("synthetic bind failure")
                path.touch(mode=0o600)

            def listen(self, backlog):
                assert backlog == 128
                if kind == "replacement":
                    path.rename(path.parent / "original-owned-inode")
                    path.write_text("replacement owner")
                raise OSError("synthetic listen failure")

            def close(self):
                closed.append(True)

        monkeypatch.setattr(server.socket, "socket", lambda *args: Socket())
        with pytest.raises((StoreError, OSError)):
            server.bind_unix(path)
        if kind == "listen_failure":
            assert not path.exists()
        elif kind == "symlink":
            assert path.is_symlink()
        else:
            expected = {
                "occupied": "another owner",
                "bind_failure": "concurrent owner",
                "replacement": "replacement owner",
            }[kind]
            assert path.read_text() == expected
        assert bool(closed) is (kind not in {"occupied", "symlink"})


@pytest.mark.parametrize(
    "kind",
    [
        "nonposix",
        "bad_port",
        "bad_host",
        "duplicate",
        "ipc_without_socket",
        "ipc_wrong_project",
        "anonymous_tcp",
        "unverified_owner",
    ],
)
def test_invalid_startup_does_not_bind_and_releases_state(service, tmp_path, monkeypatch, kind):
    _, _, _, snapshot = service
    root, socket_path = tmp_path / "startup-state", tmp_path / "ipc.sock"
    paths = [tmp_path / "project.yaml"]
    kwargs = {}
    if kind == "nonposix":
        monkeypatch.setattr(server, "os", SimpleNamespace(name="nt"))
    elif kind == "bad_port":
        kwargs["port"] = 65536
    elif kind == "bad_host":
        kwargs["host"] = "bad host"
    elif kind == "duplicate":
        paths *= 2
    elif kind == "ipc_without_socket":
        kwargs["ipc_project"] = snapshot.project_id
    elif kind == "ipc_wrong_project":
        kwargs.update(unix_socket=socket_path, ipc_project="another-project")
    else:
        data = snapshot.config.model_dump(mode="json")
        data["access"] = {"owner_only_ipc": kind == "anonymous_tcp"}
        snapshot = load_project_config(write_config(tmp_path, data), check_credentials=False)
        if kind == "unverified_owner":
            kwargs["unix_socket"] = socket_path
    monkeypatch.setattr(server, "load_project_config", lambda path: snapshot)
    monkeypatch.setattr(
        server.socket, "socket", lambda *args: pytest.fail("invalid startup bound socket")
    )
    with pytest.raises(StoreError):
        server.serve(project_paths=paths, state_root=root, **kwargs)
    assert not socket_path.exists()
    if root.exists():
        with StateLease(root):
            pass


def test_nonmain_signal_capture_is_rejected_before_changing_handlers():
    owned = server.OwnedServer.__new__(server.OwnedServer)

    def child():
        with owned.capture_signals():
            pytest.fail("worker thread captured process signals")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(StoreError, match="service_requires_main_thread"):
            pool.submit(child).result(timeout=5)


def test_signal_capture_drains_then_restores_handlers_even_on_failure(monkeypatch):
    calls, installed = [], {}
    old = {sig: object() for sig in (signal.SIGINT, signal.SIGTERM)}

    def register(sig, handler):
        calls.append((sig, handler))
        installed[sig] = handler
        return old[sig]

    owned = server.OwnedServer.__new__(server.OwnedServer)
    owned.engine = SimpleNamespace(begin_shutdown=lambda: calls.append("drain"))
    owned.handle_exit = lambda sig, frame: calls.append(("exit", sig))
    monkeypatch.setattr(server.signal, "signal", register)
    with pytest.raises(RuntimeError, match="body failure"):
        with owned.capture_signals():
            installed[signal.SIGTERM](signal.SIGTERM, None)
            raise RuntimeError("body failure")
    assert calls[2:4] == ["drain", ("exit", signal.SIGTERM)]
    assert installed == old


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_tcp_listener_is_owned_and_closed_after_server_run(service, tmp_path, monkeypatch, host):
    import socket

    if host == "::1" and not socket.has_ipv6:
        pytest.skip("platform has no IPv6")
    _, _, _, snapshot = service
    root, observed = tmp_path / "tcp-state", []
    monkeypatch.setattr(server, "load_project_config", lambda path: snapshot)

    class Server:
        def __init__(self, config, engine):
            assert config.proxy_headers is False and config.access_log is False
            assert config.limit_concurrency == engine.max_queue + engine.max_concurrency + 64
            self.engine = engine

        def run(self, *, sockets):
            observed.extend(sockets)
            assert sockets[0].getsockname()[1] > 0
            assert not self.engine.closing

    monkeypatch.setattr(server, "OwnedServer", Server)
    try:
        server.serve(project_paths=[tmp_path / "project.yaml"], state_root=root, host=host, port=0)
    except OSError as error:
        if host == "::1" and error.errno in {49, 99, 97}:
            pytest.skip("IPv6 loopback unavailable in this environment")
        raise
    assert len(observed) == 1 and observed[0].fileno() == -1
    with StateLease(root):
        pass


def test_cli_version_and_help_do_not_require_runtime_actions(capsys):
    assert cli.main(["--version"]) == 0
    from llm_gateway import __version__

    assert capsys.readouterr().out.strip() == __version__
    assert cli.main([]) == 0
    assert "--project-config" not in capsys.readouterr().err


def test_cli_rejects_service_platform_before_loading_configuration(monkeypatch, capsys):
    monkeypatch.setattr(cli, "os", SimpleNamespace(name="nt"))
    assert cli.main(["serve", "--project-config", "/unused", "--state-dir", "/unused"]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "service_requires_posix"


def test_cli_missing_service_extra_is_a_safe_error(monkeypatch, capsys):
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "llm_gateway.service.server":
            raise ModuleNotFoundError("synthetic missing service dependency")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    assert cli.main(["serve", "--project-config", "/unused", "--state-dir", "/unused"]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "service_extra_required"


def test_cli_unknown_startup_exception_does_not_echo_details(monkeypatch, capsys):
    def fail(**kwargs):
        raise ValueError("secret exception canary: private value")

    monkeypatch.setattr(server, "serve", fail)
    assert cli.main(["serve", "--project-config", "/unused", "--state-dir", "/unused"]) == 2
    output = capsys.readouterr()
    assert json.loads(output.err)["code"] == "service_start_failed"
    assert "canary" not in output.err


def test_cli_backup_restore_uses_real_private_ledger_and_preserves_restore_fence(
    service, tmp_path, capsys
):
    _, engine, _, _ = service
    backup, restored = tmp_path / "backup.sqlite", tmp_path / "restored"
    assert (
        cli.main(
            ["state", "backup", "--state-dir", str(engine.store.root), "--output", str(backup)]
        )
        == 0
    )
    assert cli.main(["state", "restore", "--state-dir", str(restored), "--input", str(backup)]) == 0
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[0]["admission_fenced_on_restore"] is True
    assert reports[1] == {"status": "restored", "admission_fenced": True}
    with StateStore(restored).connection() as db:
        assert db.execute("SELECT COUNT(*) FROM config_snapshots").fetchone()[0] == 1
        assert db.execute("SELECT 1 FROM runtime_flags WHERE name='admission_fenced'").fetchone()
