import json
import sqlite3

import pytest
from test_engine import configured
from test_ledger import request

from llm_gateway.service.cli import main
from llm_gateway.storage import StateStore, StoreError
from llm_gateway.storage.backup import backup, restore
from llm_gateway.storage.lease import StateLease
from llm_gateway.storage.ledger import Ledger

pytestmark = pytest.mark.offline


def test_backup_preserves_charges_and_restore_fences_admission(tmp_path):
    engine, authority, _ = configured(tmp_path)
    engine.run(authority, request())
    before = engine.ledger.report(authority.project)
    report = backup(engine.store.root, tmp_path / "backup.sqlite")
    assert report["attempts"] == 1 and report["admission_fenced_on_restore"]
    assert (tmp_path / "backup.sqlite").stat().st_mode & 0o077 == 0
    restore(tmp_path / "backup.sqlite", tmp_path / "restored")
    restored = StateStore(tmp_path / "restored")
    assert Ledger(restored).report(authority.project) == before
    with restored.connection() as db:
        flags = dict(db.execute("SELECT name,value FROM runtime_flags"))
        assert flags["admission_fenced"] == "restore_requires_gap_resolution"
    # The original ledger is not fenced by making a backup.
    engine.run(authority, request("two"))
    assert engine.ledger.report(authority.project)["attempts"] == 2
    assert Ledger(restored).report(authority.project)["attempts"] == 1


def test_backup_refuses_running_state_overwrite_and_unsupported_version(tmp_path):
    engine, _, _ = configured(tmp_path)
    output = tmp_path / "backup.sqlite"
    with StateLease(engine.store.root), pytest.raises(StoreError, match="owned"):
        backup(engine.store.root, output)
    backup(engine.store.root, output)
    with pytest.raises(FileExistsError):
        backup(engine.store.root, output)
    with sqlite3.connect(output) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(StoreError, match="schema_version"):
        restore(output, tmp_path / "restored")


def test_cli_state_errors_do_not_expose_paths_and_fresh_target_only(tmp_path, capsys):
    assert (
        main(
            [
                "state",
                "backup",
                "--state-dir",
                str(tmp_path / "missing"),
                "--output",
                str(tmp_path / "private-name"),
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["code"] == "state_operation_failed"
    engine, _, _ = configured(tmp_path)
    output = tmp_path / "backup.sqlite"
    backup(engine.store.root, output)
    with pytest.raises(FileExistsError):
        restore(output, engine.store.root)
