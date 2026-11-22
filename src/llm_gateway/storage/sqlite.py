"""T023: private local SQLite, explicit transactions and forward-version guards."""

import os
import re
import sqlite3
import stat
import subprocess
from contextlib import closing, contextmanager
from functools import lru_cache
from pathlib import Path

SCHEMA_VERSION = 3
MIGRATION_3 = """
CREATE TABLE retained_prompts (
 project_id TEXT NOT NULL, principal_id TEXT NOT NULL, request_id TEXT NOT NULL,
 payload TEXT, expires_at TEXT NOT NULL,
 PRIMARY KEY(project_id,principal_id,request_id),
 FOREIGN KEY(project_id,principal_id,request_id)
 REFERENCES requests(project_id,principal_id,request_id)
);
"""
MIGRATION_2 = """
CREATE TABLE retained_results (
 project_id TEXT NOT NULL, principal_id TEXT NOT NULL, request_id TEXT NOT NULL,
 config_digest TEXT NOT NULL, cache_key TEXT NOT NULL, payload TEXT, expires_at TEXT NOT NULL,
 PRIMARY KEY(project_id,principal_id,request_id),
 FOREIGN KEY(project_id,principal_id,request_id)
 REFERENCES requests(project_id,principal_id,request_id)
);
CREATE INDEX retained_cache ON retained_results(project_id,principal_id,config_digest,cache_key);
CREATE TABLE fake_sequences (
 project_id TEXT NOT NULL, principal_id TEXT NOT NULL, config_digest TEXT NOT NULL,
 sequence_key TEXT NOT NULL, position INTEGER NOT NULL,
 PRIMARY KEY(project_id,principal_id,config_digest,sequence_key)
);
"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS config_snapshots (
 project_id TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL,
 fixtures_payload TEXT NOT NULL, capability_revision TEXT NOT NULL,
 PRIMARY KEY(project_id,digest), CHECK(length(digest)=64)
);
CREATE TABLE IF NOT EXISTS active_configs (
 project_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
 FOREIGN KEY(project_id,digest) REFERENCES config_snapshots(project_id,digest)
);
CREATE TABLE IF NOT EXISTS requests (
 project_id TEXT NOT NULL, principal_id TEXT NOT NULL, request_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, config_digest TEXT NOT NULL, alias TEXT NOT NULL,
 operation TEXT NOT NULL, context_json TEXT NOT NULL, limits_json TEXT NOT NULL,
 execution_status TEXT NOT NULL DEFAULT 'in_progress',
 output_status TEXT NOT NULL DEFAULT 'pending',
 trace_id TEXT NOT NULL, created_at TEXT NOT NULL, deadline_at TEXT NOT NULL,
 result_ref TEXT, cancelled INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(project_id,principal_id,request_id),
 FOREIGN KEY(project_id,config_digest) REFERENCES config_snapshots(project_id,digest)
);
CREATE TABLE IF NOT EXISTS attempts (
 attempt_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, principal_id TEXT NOT NULL,
 request_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>0),
 state TEXT NOT NULL, route_json TEXT NOT NULL, reserve_usd TEXT NOT NULL,
 protected_usd TEXT NOT NULL, measured_usd TEXT, estimated_usd TEXT,
 evidence_status TEXT NOT NULL DEFAULT 'unavailable', usage_json TEXT,
 model_json TEXT, generation_id TEXT, error_code TEXT,
 output_status TEXT NOT NULL DEFAULT 'pending',
 usage_complete INTEGER NOT NULL DEFAULT 0, unknown_hold INTEGER NOT NULL DEFAULT 0,
 UNIQUE(project_id,principal_id,request_id,ordinal),
 FOREIGN KEY(project_id,principal_id,request_id)
 REFERENCES requests(project_id,principal_id,request_id)
);
CREATE TABLE IF NOT EXISTS budget_buckets (
 bucket_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, policy_id TEXT NOT NULL,
 scope_id TEXT NOT NULL, period_key TEXT NOT NULL, start_at TEXT, end_at TEXT,
 cap_usd TEXT NOT NULL, protected_usd TEXT NOT NULL DEFAULT '0',
 UNIQUE(project_id,policy_id,scope_id,period_key)
);
CREATE TABLE IF NOT EXISTS allocations (
 attempt_id TEXT NOT NULL, bucket_id TEXT NOT NULL, protected_usd TEXT NOT NULL,
 PRIMARY KEY(attempt_id,bucket_id), FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id),
 FOREIGN KEY(bucket_id) REFERENCES budget_buckets(bucket_id)
);
CREATE TABLE IF NOT EXISTS evidence (
 project_id TEXT NOT NULL, evidence_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
 digest TEXT NOT NULL, payload TEXT NOT NULL, observed_at TEXT NOT NULL,
 PRIMARY KEY(project_id,evidence_id), FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id)
);
CREATE TABLE IF NOT EXISTS quota_slots (
 project_id TEXT NOT NULL, policy_id TEXT NOT NULL, period_key TEXT NOT NULL,
 request_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
 PRIMARY KEY(project_id,policy_id,period_key),
 FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id)
);
CREATE TABLE IF NOT EXISTS trace_outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
 request_id TEXT, event_json TEXT NOT NULL, exported INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runtime_flags (name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class StoreError(ValueError):
    pass


def _schema_objects(connection):
    return tuple(
        (kind, name, table, " ".join(sql.split()) if sql else None)
        for kind, name, table, sql in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )


@lru_cache(maxsize=3)
def _expected_schema(version):
    with closing(sqlite3.connect(":memory:")) as expected:
        script = SCHEMA
        if version >= 2:
            script += MIGRATION_2
        if version >= 3:
            script += MIGRATION_3
        expected.executescript(script)
        return _schema_objects(expected)


def verify_schema(connection, version=SCHEMA_VERSION):
    """A version number alone cannot prove required constraints/tables exist."""
    if version not in {1, 2, 3} or _schema_objects(connection) != _expected_schema(version):
        raise StoreError("state_schema_layout_unrecognized")


def verify_private_ancestors(root: Path) -> None:
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise StoreError("state_directory_must_be_owner_only")
    for path in (root, *root.parents):
        info = path.stat()
        if info.st_uid not in {os.getuid(), 0}:
            raise StoreError("state_ancestor_owner_untrusted")
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise StoreError("state_ancestor_writable")


def filesystem_type(path: Path) -> str:
    """Reject known remote filesystems; resolve the longest mounted parent."""
    mounts = []
    mountinfo = Path("/proc/self/mountinfo")
    if mountinfo.exists():
        for line in mountinfo.read_text().splitlines():
            before, after = line.split(" - ", 1)
            target = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), before.split()[4])
            mounts.append((Path(target), after.split()[0]))
    elif Path("/sbin/mount").exists():
        try:
            result = subprocess.run(
                ["/sbin/mount"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
                env={"LANG": "C", "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError):
            raise StoreError("state_filesystem_unverified") from None
        for line in result.stdout.splitlines():
            match = re.fullmatch(r"(.*) \(([^, )]+)(?:,[^)]*)?\)", line)
            if not match:
                raise StoreError("state_filesystem_unverified")
            # Apple prints source and mountpoint verbatim around ' on '. Names
            # may contain that delimiter too. Only an absolute suffix can be
            # the real mountpoint; ambiguity relevant to this state fails closed.
            targets = {
                Path(match[1][marker.start() + 4 :])
                for marker in re.finditer(r"(?= on )", match[1])
                if Path(match[1][marker.start() + 4 :]).is_absolute()
            }
            if len(targets) != 1:
                if not targets or any(
                    target == path or target in path.parents for target in targets
                ):
                    raise StoreError("state_filesystem_unverified")
                continue
            mounts.append((targets.pop(), match[2]))
    else:
        raise StoreError("state_filesystem_unverified")
    candidates = [
        (len(str(target)), kind)
        for target, kind in mounts
        if path == target or target in path.parents
    ]
    if not candidates:
        raise StoreError("state_filesystem_unverified")
    return max(candidates)[1]


class StateStore:
    def __init__(self, root: Path):
        if root.is_symlink():
            raise StoreError("state_directory_symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = root.resolve()
        info = self.root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise StoreError("state_directory_must_be_owner_only")
        verify_private_ancestors(self.root)
        if filesystem_type(self.root) not in {
            "apfs",
            "hfs",
            "hfsplus",
            "ext2",
            "ext3",
            "ext4",
            "xfs",
            "btrfs",
            "zfs",
            "tmpfs",
            "overlay",
            "aufs",
            "ramfs",
            "f2fs",
            "ufs",
        }:
            raise StoreError("state_requires_local_filesystem")
        if os.path.lexists(self.root / "restore.incomplete"):
            raise StoreError("state_restore_incomplete")
        self.path = self.root / "gateway.sqlite"
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        if self.path.is_symlink():
            raise StoreError("state_database_symlink")
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise StoreError("state_database_must_be_owner_only")
        with self.connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2, SCHEMA_VERSION}:
                raise StoreError("state_schema_version_unsupported")
            if version == 0:
                tables = connection.execute("SELECT name FROM sqlite_master").fetchall()
                if tables:
                    raise StoreError("state_unversioned_nonempty")
            else:
                verify_schema(connection, version)
            if version < SCHEMA_VERSION:
                migrations = SCHEMA if version == 0 else ""
                migrations += MIGRATION_2 if version < 2 else ""
                migrations += MIGRATION_3 if version < 3 else ""
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + migrations
                    + f"PRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;"
                )
            verify_schema(connection)

    @contextmanager
    def connection(self):
        verify_private_ancestors(self.root)
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
