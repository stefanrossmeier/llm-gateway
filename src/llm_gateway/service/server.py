"""Executable service ownership, listener setup and conservative startup recovery."""

import json
import os
import signal
import socket
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from llm_gateway.config import load_project_config
from llm_gateway.providers.process import OwnedProviderProcess
from llm_gateway.storage import StateStore, StoreError
from llm_gateway.storage.lease import StateLease
from llm_gateway.storage.sqlite import verify_private_ancestors

from .api import DEFAULT_BODY, create_app, validate_body_limit
from .engine import Engine


class OwnedServer(uvicorn.Server):
    def __init__(self, config, engine):
        super().__init__(config)
        self.engine = engine

    @contextmanager
    def capture_signals(self):
        if threading.current_thread() is not threading.main_thread():
            raise StoreError("service_requires_main_thread")

        def stop(signum, frame):
            self.engine.begin_shutdown()
            self.handle_exit(signum, frame)

        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        # Do not re-raise the signal before our owned-process/state cleanup runs.


def bind_unix(path: Path):
    if not path.is_absolute() or len(os.fsencode(path)) > 100:
        raise StoreError("unix_socket_requires_short_absolute_path")
    verify_private_ancestors(path.parent)
    # Never unlink an existing socket; a different server may own it. Cleanup of
    # a stale path is a separate explicit operator action after ownership review.
    if path.exists() or path.is_symlink():
        raise StoreError("unix_socket_path_exists")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity = None
    try:
        listener.bind(str(path))
        identity = path.lstat().st_ino
        path.chmod(0o600)
        listener.listen(128)
        return listener
    except BaseException:
        listener.close()
        if identity is not None and path.exists() and path.lstat().st_ino == identity:
            path.unlink()
        raise


def serve(
    *,
    project_paths,
    state_root,
    host="127.0.0.1",
    port=8000,
    unix_socket=None,
    ipc_project=None,
    deployment_proxy=None,
    max_concurrency=8,
    max_queue=256,
    max_body_bytes=DEFAULT_BODY,
):
    if os.name != "posix":
        raise StoreError("service_requires_posix")
    if not 0 <= port <= 65535 or not host or any(c.isspace() for c in host):
        raise StoreError("listener_invalid")
    validate_body_limit(max_body_bytes)
    with StateLease(state_root) as lease:
        store = StateStore(lease.root)
        engine = Engine(
            store,
            provider=OwnedProviderProcess(store.root, deployment_proxy=deployment_proxy),
            max_concurrency=max_concurrency,
            max_queue=max_queue,
        )
        listener = None
        socket_identity = None
        try:
            snapshots = [load_project_config(path) for path in project_paths]
            if len({s.project_id for s in snapshots}) != len(snapshots):
                raise StoreError("duplicate_project_configuration")
            if unix_socket is not None and ipc_project is None and len(snapshots) == 1:
                ipc_project = snapshots[0].project_id
            if ipc_project and (
                unix_socket is None or ipc_project not in {s.project_id for s in snapshots}
            ):
                raise StoreError("owner_ipc_project_invalid")
            for snapshot in snapshots:
                if not snapshot.config.access.principals and not (
                    unix_socket is not None
                    and ipc_project == snapshot.project_id
                    and snapshot.config.access.owner_only_ipc
                ):
                    raise StoreError("project_requires_bearer_or_dedicated_owner_socket")
            engine.ledger.recover()
            for snapshot in snapshots:
                engine.registry.activate(snapshot)
            if unix_socket:
                listener = bind_unix(unix_socket)
                socket_identity = unix_socket.lstat().st_ino
            else:
                family = socket.AF_INET6 if ":" in host else socket.AF_INET
                listener = socket.socket(family, socket.SOCK_STREAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((host, port))
                listener.listen(128)
            app = create_app(
                engine,
                project_configs={
                    s.project_id: path for s, path in zip(snapshots, project_paths, strict=True)
                },
                owner_socket=listener if unix_socket else None,
                ipc_project=ipc_project,
                max_body_bytes=max_body_bytes,
            )
            print(
                json.dumps(
                    {
                        "status": "listening",
                        "projects": [s.project_id for s in snapshots],
                        "listener": str(listener.getsockname()),
                    }
                ),
                flush=True,
            )
            server = OwnedServer(
                uvicorn.Config(
                    app,
                    access_log=False,
                    log_level="warning",
                    server_header=False,
                    timeout_graceful_shutdown=5,
                    limit_concurrency=engine.max_queue + engine.max_concurrency + 64,
                    proxy_headers=False,
                ),
                engine,
            )
            server.run(sockets=[listener])
        finally:
            engine.shutdown()
            if listener is not None:
                listener.close()
                if (
                    unix_socket
                    and unix_socket.exists()
                    and unix_socket.lstat().st_ino == socket_identity
                    and stat.S_ISSOCK(unix_socket.lstat().st_mode)
                ):
                    unix_socket.unlink()
