"""COST durable state: reject altered layouts before migration or restoration."""

import sqlite3

import pytest

from llm_gateway.storage.backup import checked_database, restore
from llm_gateway.storage.sqlite import (
    MIGRATION_2,
    MIGRATION_3,
    SCHEMA,
    SCHEMA_VERSION,
    StateStore,
    StoreError,
)

pytestmark = pytest.mark.offline


def database(tmp_path, script):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    path = root / "gateway.sqlite"
    path.touch(mode=0o600)
    with sqlite3.connect(path) as connection:
        connection.executescript(script)
    return root, path


@pytest.mark.parametrize("version", [1, 2, 3])
def test_every_supported_original_layout_migrates_with_history_intact(tmp_path, version):
    script = SCHEMA + (MIGRATION_2 if version >= 2 else "") + (MIGRATION_3 if version >= 3 else "")
    root, _ = database(tmp_path, script + f"PRAGMA user_version={version};")
    with sqlite3.connect(root / "gateway.sqlite") as connection:
        connection.execute("INSERT INTO runtime_flags VALUES ('history-canary','preserved')")
    store = StateStore(root)
    with store.connection() as connection:
        checked_database(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            connection.execute(
                "SELECT value FROM runtime_flags WHERE name='history-canary'"
            ).fetchone()[0]
            == "preserved"
        )


@pytest.mark.parametrize(
    "change",
    [
        "missing_table",
        "missing_index",
        "extra_view",
        "extra_trigger",
        "weakened_constraint",
    ],
)
def test_same_version_cannot_hide_changed_durable_schema(tmp_path, change):
    script = SCHEMA + MIGRATION_2 + MIGRATION_3 + f"PRAGMA user_version={SCHEMA_VERSION};"
    if change == "weakened_constraint":
        script = script.replace("CHECK(ordinal>0)", "CHECK(ordinal>=0)")
    root, path = database(tmp_path, script)
    with sqlite3.connect(path) as connection:
        if change == "missing_table":
            connection.execute("DROP TABLE evidence")
        elif change == "missing_index":
            connection.execute("DROP INDEX retained_cache")
        elif change == "extra_view":
            connection.execute("CREATE VIEW custom_view AS SELECT 1")
        elif change == "extra_trigger":
            connection.execute(
                "CREATE TRIGGER custom_trigger AFTER INSERT ON runtime_flags BEGIN SELECT 1; END"
            )
        connection.execute("INSERT INTO runtime_flags VALUES ('history-canary','preserved')")
    with pytest.raises(StoreError, match="schema_layout_unrecognized"):
        StateStore(root)
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT value FROM runtime_flags WHERE name='history-canary'"
            ).fetchone()[0]
            == "preserved"
        )


def test_unrecognized_old_layout_is_rejected_before_upgrading_its_version(tmp_path):
    root, path = database(
        tmp_path, SCHEMA.replace("CHECK(ordinal>0)", "CHECK(ordinal>=0)") + "PRAGMA user_version=1;"
    )
    with pytest.raises(StoreError, match="schema_layout_unrecognized"):
        StateStore(root)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='retained_prompts'"
            ).fetchone()
            is None
        )


def test_unversioned_view_is_not_an_empty_new_database(tmp_path):
    root, path = database(tmp_path, "CREATE VIEW existing_view AS SELECT 1;")
    with pytest.raises(StoreError, match="unversioned_nonempty"):
        StateStore(root)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_unversioned_leftover_sqlite_metadata_is_not_a_pristine_new_ledger(tmp_path):
    root, _ = database(
        tmp_path,
        "CREATE TABLE old_state(id INTEGER PRIMARY KEY AUTOINCREMENT); DROP TABLE old_state;",
    )
    with pytest.raises(StoreError, match="unversioned_nonempty"):
        StateStore(root)


def test_restore_rejects_a_version_only_database_before_admitting_any_state(tmp_path):
    _, path = database(tmp_path, f"PRAGMA user_version={SCHEMA_VERSION};")
    target = tmp_path / "restored"
    with pytest.raises(StoreError, match="schema_layout_unrecognized"):
        restore(path, target)
    assert not target.exists()


def test_partial_restore_of_a_valid_unfenced_source_cannot_start_admission(tmp_path, monkeypatch):
    import llm_gateway.storage.backup as backup_module

    source = StateStore(tmp_path / "source")
    target = tmp_path / "restored"
    original_connect = sqlite3.connect

    class FailAfterCopy(sqlite3.Connection):
        def backup(self, destination, **kwargs):
            super().backup(destination, **kwargs)
            raise OSError("synthetic copy completion failure")

    monkeypatch.setattr(
        backup_module.sqlite3,
        "connect",
        lambda *args, **kwargs: original_connect(*args, factory=FailAfterCopy, **kwargs),
    )
    with pytest.raises(OSError):
        restore(source.path, target)
    assert (target / "restore.incomplete").exists()
    assert (target / "restore.incomplete").stat().st_mode & 0o077 == 0
    with pytest.raises(StoreError, match="state_restore_incomplete"):
        StateStore(target)
    # The original remains usable and was never given a destination fence.
    with original_connect(source.path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM runtime_flags WHERE name='admission_fenced'"
            ).fetchone()
            is None
        )
