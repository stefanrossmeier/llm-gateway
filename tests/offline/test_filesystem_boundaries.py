"""Local-state classification works independently of the test runner's OS."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_gateway.storage import StateStore, StoreError
from llm_gateway.storage import sqlite as storage

pytestmark = pytest.mark.offline
ROOT = "device on / (apfs, local)\n"


def mac_table(monkeypatch, table, *, available=True, failure=None):
    paths, calls = [], []

    class Probe:
        def __init__(self, value):
            self.value = value

        def exists(self):
            return self.value == "/sbin/mount" and available

    def path(value):
        if str(value) in {"/proc/self/mountinfo", "/sbin/mount"}:
            paths.append(str(value))
            return Probe(str(value))
        return Path(value)

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if failure:
            raise failure
        return SimpleNamespace(stdout=table)

    monkeypatch.setattr(storage, "Path", path)
    monkeypatch.setattr(storage.subprocess, "run", run)
    return calls


def test_mac_longest_mount_match_keeps_remote_names_containing_separator_text(monkeypatch):
    calls = mac_table(
        monkeypatch,
        ROOT
        + "remote on /Volumes/remote on disk (nfs, nodev)\n"
        + "local on /Volumes/remote on disk/local (apfs, local)\n",
    )
    assert storage.filesystem_type(Path("/Volumes/remote on disk")) == "nfs"
    assert storage.filesystem_type(Path("/Volumes/remote on disk/state")) == "nfs"
    assert storage.filesystem_type(Path("/Volumes/remote on disk/local/state")) == "apfs"
    assert len(calls) == 3


def test_remote_state_with_separator_in_name_is_never_opened_as_sqlite(tmp_path, monkeypatch):
    root = tmp_path / "state on disk"
    mac_table(monkeypatch, ROOT + f"remote on {root} (nfs, nodev)\n")
    with pytest.raises(StoreError, match="state_requires_local_filesystem"):
        StateStore(root)
    assert not (root / "gateway.sqlite").exists()


@pytest.mark.parametrize("path", ["/Volumes/private on /unclear/state", "/unclear/state"])
def test_ambiguous_relevant_mountpoint_fails_closed(monkeypatch, path):
    mac_table(monkeypatch, ROOT + "source on /Volumes/private on /unclear (apfs, local)\n")
    with pytest.raises(StoreError, match="state_filesystem_unverified"):
        storage.filesystem_type(Path(path))


def test_ambiguity_that_cannot_cover_state_does_not_override_its_filesystem(monkeypatch):
    mac_table(monkeypatch, ROOT + "source on /unrelated on /elsewhere (nfs, nodev)\n")
    assert storage.filesystem_type(Path("/private/state")) == "apfs"


@pytest.mark.parametrize(
    "table", ["", "malformed\n", "source on relative (apfs)\n", "source on /elsewhere (apfs)\n"]
)
def test_missing_unparseable_or_nonmatching_mount_evidence_is_not_local(monkeypatch, table):
    mac_table(monkeypatch, table)
    with pytest.raises(StoreError, match="state_filesystem_unverified"):
        storage.filesystem_type(Path("/private/state"))


def test_mount_helper_has_no_inherited_secrets_stdin_or_unbounded_runtime(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-inference-canary")
    monkeypatch.setenv("GITHUB_TOKEN", "unrelated-private-canary")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/untrusted/loader")
    calls = mac_table(monkeypatch, ROOT)
    assert storage.filesystem_type(Path("/private/state")) == "apfs"
    command, kwargs = calls[0]
    assert command == ["/sbin/mount"]
    assert kwargs == {
        "capture_output": True,
        "text": True,
        "check": True,
        "timeout": 5,
        "stdin": subprocess.DEVNULL,
        "env": {"LANG": "C", "LC_ALL": "C"},
    }


@pytest.mark.parametrize(
    "failure",
    [
        OSError("private-canary"),
        subprocess.TimeoutExpired(["/sbin/mount"], 5),
        subprocess.CalledProcessError(1, ["/sbin/mount"], stderr="private-canary"),
    ],
)
def test_mount_helper_failures_are_safe_unverified_state(monkeypatch, failure):
    mac_table(monkeypatch, ROOT, failure=failure)
    with pytest.raises(StoreError, match="state_filesystem_unverified") as caught:
        storage.filesystem_type(Path("/private/state"))
    assert "canary" not in str(caught.value)


def test_absent_proc_and_mount_utility_cannot_claim_local_state(monkeypatch):
    calls = mac_table(monkeypatch, ROOT, available=False)
    with pytest.raises(StoreError, match="state_filesystem_unverified"):
        storage.filesystem_type(Path("/private/state"))
    assert calls == []


@pytest.mark.parametrize("table", ["", "1 0 0:1 / /elsewhere rw - ext4 root rw\n"])
def test_linux_empty_or_nonmatching_mount_table_is_unverified(monkeypatch, table):
    class Proc:
        def exists(self):
            return True

        def read_text(self):
            return table

    monkeypatch.setattr(
        storage,
        "Path",
        lambda value: Proc() if str(value) == "/proc/self/mountinfo" else Path(value),
    )
    monkeypatch.setattr(
        storage.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("proc lookup spawned utility"),
    )
    with pytest.raises(StoreError, match="state_filesystem_unverified"):
        storage.filesystem_type(Path("/private/state"))


def test_actual_local_state_path_may_contain_separator_words(tmp_path):
    store = StateStore(tmp_path / "state on local disk")
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
