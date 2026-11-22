"""SEC private-state/credential/restore trust boundaries, no provider inference."""

import os
import socket
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_config import private_file
from test_engine import configured

from llm_gateway import credentials
from llm_gateway.credentials import SecretUnavailable, read_private_secret
from llm_gateway.service.auth import Authenticator
from llm_gateway.service.engine import EngineError
from llm_gateway.storage import StateStore, StoreError
from llm_gateway.storage import sqlite as storage
from llm_gateway.storage.backup import checked_database, private_input
from llm_gateway.storage.lease import StateLease

pytestmark = pytest.mark.offline


@pytest.mark.parametrize(
    "kind", ["relative", "symlink", "fifo", "public", "empty", "multiline", "nonutf8", "oversize"]
)
def test_bad_credential_file_never_returns_partial_secret(tmp_path, kind):
    path = private_file(tmp_path, "key", "synthetic-private-canary")
    if kind == "relative":
        path = Path("relative-key")
    elif kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif kind == "public":
        path.chmod(0o644)
    else:
        path.write_bytes(
            {
                "empty": b"  \n",
                "multiline": b"first\nsecond",
                "nonutf8": b"\xff",
                "oversize": b"x" * 16385,
            }[kind]
        )
    with pytest.raises(SecretUnavailable) as error:
        read_private_secret(str(path))
    assert str(error.value) == "credential_unavailable"


def test_nonposix_secret_reader_is_explicitly_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "os", SimpleNamespace(name="nt"))
    with pytest.raises(SecretUnavailable):
        read_private_secret(str(tmp_path / "key"))


def test_secret_file_owner_and_growth_are_checked_on_open_descriptor(tmp_path, monkeypatch):
    path = private_file(tmp_path, "key", "synthetic")
    original = os.fstat
    with monkeypatch.context() as patch:
        patch.setattr(
            os,
            "fstat",
            lambda fd: SimpleNamespace(
                st_mode=original(fd).st_mode, st_size=9, st_uid=os.getuid() + 10000
            ),
        )
        with pytest.raises(SecretUnavailable):
            read_private_secret(str(path))
    # Model a file growing after fstat: the bounded read must still reject it.
    monkeypatch.setattr(os, "read", lambda fd, size: b"x" * size)
    with pytest.raises(SecretUnavailable):
        read_private_secret(str(path))


@pytest.mark.parametrize(
    "kind", ["root_symlink", "public_root", "db_symlink", "public_db", "fifo_db", "remote_fs"]
)
def test_state_refuses_untrusted_roots_and_database_files(tmp_path, monkeypatch, kind):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    path = root / "gateway.sqlite"
    if kind == "root_symlink":
        link = tmp_path / "link"
        link.symlink_to(root)
        root = link
    elif kind == "public_root":
        root.chmod(0o755)
    elif kind == "db_symlink":
        path.symlink_to(private_file(tmp_path, "target", ""))
    elif kind == "public_db":
        path.write_bytes(b"")
        path.chmod(0o644)
    elif kind == "fifo_db":
        os.mkfifo(path, 0o600)
    else:
        monkeypatch.setattr(storage, "filesystem_type", lambda _: "nfs4")
    with pytest.raises(StoreError):
        StateStore(root)


def test_untrusted_ancestor_and_nonsticky_writable_parent_are_refused(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    root = parent / "state"
    root.mkdir(mode=0o700)
    parent.chmod(0o777)
    try:
        with pytest.raises(StoreError, match="ancestor_writable"):
            storage.verify_private_ancestors(root)
    finally:
        parent.chmod(0o700)
    original = Path.stat

    def changed_owner(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        return (
            SimpleNamespace(st_uid=os.getuid() + 10000, st_mode=info.st_mode)
            if path == parent
            else info
        )

    monkeypatch.setattr(Path, "stat", changed_owner)
    with pytest.raises(StoreError, match="ancestor_owner_untrusted"):
        storage.verify_private_ancestors(root)


def test_linux_mount_matching_uses_longest_parent_and_unescapes_spaces(monkeypatch):
    # No mount or host filesystem mutation: supply the kernel mount-table input.
    original = storage.Path

    class MountInfo:
        def exists(self):
            return True

        def read_text(self):
            return (
                "1 0 0:1 / / rw - ext4 root rw\n"
                "2 1 0:2 / /private\\040mount rw - nfs remote rw\n"
                "3 2 0:3 / /private\\040mount/local rw - tmpfs tmp rw\n"
            )

    monkeypatch.setattr(
        storage,
        "Path",
        lambda value: MountInfo() if str(value) == "/proc/self/mountinfo" else original(value),
    )
    assert storage.filesystem_type(original("/private mount/remote")) == "nfs"
    assert storage.filesystem_type(original("/private mount/local/state")) == "tmpfs"


def test_backup_checks_private_input_and_actual_foreign_keys(tmp_path):
    path = private_file(tmp_path, "backup.sqlite", "")
    path.chmod(0o644)
    with pytest.raises(StoreError, match="backup_file_must_be_private_regular"):
        private_input(path)
    store = StateStore(tmp_path / "state")
    # Recognized schema, but an owner-modified row violates its real FK.
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("INSERT INTO active_configs VALUES ('orphan','missing')")
        db.commit()
        with pytest.raises(StoreError, match="backup_foreign_keys_failed"):
            checked_database(db)


def test_lease_close_is_idempotent_and_releases_actual_lock(tmp_path):
    lease = StateLease(tmp_path / "state")
    lease.close()
    lease.close()
    with StateLease(tmp_path / "state") as acquired:
        assert acquired.fd is not None


def test_owner_ipc_rejects_tcp_and_public_unix_socket(tmp_path):
    with socket.socket() as tcp:
        with pytest.raises(EngineError, match="owner_ipc_requires_unix_socket"):
            Authenticator(None, owner_socket=tcp)
    with tempfile.TemporaryDirectory(prefix="lg-ipc-", dir="/tmp") as directory:
        private = Path(directory).resolve()
        with socket.socket(socket.AF_UNIX) as unix:
            unix.bind(str(private / "gateway.sock"))
            (private / "gateway.sock").chmod(0o666)
            with pytest.raises(EngineError, match="owner_ipc_socket_not_private"):
                Authenticator(None, owner_socket=unix)


def test_short_bearer_or_stored_token_and_ungranted_ipc_are_rejected(tmp_path):
    engine, _, snapshot = configured(tmp_path)
    token = private_file(tmp_path, "token", "short")
    data = snapshot.config.model_copy(deep=True)
    from llm_gateway.config.models import PrincipalConfig

    data.access.principals["short"] = PrincipalConfig(token_file=str(token))

    class Registry:
        store = engine.store

        def get(self, _project):
            return SimpleNamespace(config=data, digest=snapshot.digest)

    auth = Authenticator(Registry(), ipc_project="project-a")
    for value in ("Bearer short", "Bearer " + "x" * 48):
        with pytest.raises(EngineError, match="authentication_failed"):
            auth.authenticate(value, client=("127.0.0.1", 1))
    # A verified private transport still needs an explicit per-project grant.
    data = data.model_copy(
        update={"access": data.access.model_copy(update={"owner_only_ipc": False})}
    )
    auth.ipc_verified = True
    with pytest.raises(EngineError, match="authentication_required"):
        auth.authenticate(None, client=None)
