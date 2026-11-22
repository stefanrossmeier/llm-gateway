"""Offline consistent backups; every restored copy starts with admission fenced."""

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from .lease import StateLease
from .sqlite import SCHEMA_VERSION, StateStore, StoreError, verify_private_ancestors, verify_schema


def private_input(path):
    verify_private_ancestors(path.parent)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise StoreError("backup_file_must_be_private_regular")


def checked_database(connection):
    if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise StoreError("backup_schema_version_unsupported")
    verify_schema(connection)
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise StoreError("backup_integrity_failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise StoreError("backup_foreign_keys_failed")


def fence(connection, checkpoint):
    with connection:
        for key, value in [
            ("admission_fenced", "restore_requires_gap_resolution"),
            ("restore_checkpoint", json.dumps(checkpoint, sort_keys=True)),
        ]:
            connection.execute(
                "INSERT INTO runtime_flags VALUES (?,?) ON CONFLICT(name) "
                "DO UPDATE SET value=excluded.value",
                (key, value),
            )


def backup(state_root: Path, output: Path):
    if not (state_root / "gateway.sqlite").is_file():
        raise StoreError("backup_state_missing")
    verify_private_ancestors(output.parent)
    with StateLease(state_root):
        store = StateStore(state_root)
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        try:
            with store.connection() as source, closing(sqlite3.connect(output)) as destination:
                source.backup(destination)
                checked_database(destination)
                row = destination.execute(
                    "SELECT COUNT(*),MAX(r.created_at) FROM attempts a JOIN requests r "
                    "USING(project_id,principal_id,request_id)"
                ).fetchone()
                checkpoint = {
                    "created_at": datetime.now(UTC).isoformat(),
                    "attempts": row[0],
                    "latest_attempt_at": row[1],
                }
                fence(destination, checkpoint)
                destination.execute("PRAGMA journal_mode=DELETE")
            with output.open("rb") as file:
                os.fsync(file.fileno())
            directory = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    return {
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "admission_fenced_on_restore": True,
        **checkpoint,
    }


def restore(source: Path, state_root: Path):
    private_input(source)
    with closing(
        sqlite3.connect(f"file:{quote(str(source.resolve()))}?mode=ro&immutable=1", uri=True)
    ) as original:
        checked_database(original)  # Validate before creating any destination.
        # Explicit fresh target only. Never overwrite a historical ledger.
        state_root.mkdir(mode=0o700)
        marker = state_root / "restore.incomplete"
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, b"restore_incomplete\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        directory = os.open(state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        with StateLease(state_root):
            target = state_root / "gateway.sqlite"
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            with closing(sqlite3.connect(target)) as destination:
                original.backup(destination)
                checked_database(destination)
                fence(
                    destination,
                    {
                        "restored_at": datetime.now(UTC).isoformat(),
                        "backup_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    },
                )
            # The copied ledger and durable admission fence now exist. A crash
            # before this point leaves the marker, which startup always rejects.
            marker.unlink()
            directory = os.open(state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            StateStore(state_root)
    return {"status": "restored", "admission_fenced": True}
