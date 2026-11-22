"""Private trace sinks must fail without blocking or acknowledging lost events."""

import json
import os
import subprocess
import sys

import pytest
from test_engine import configured

from llm_gateway.storage import StoreError
from llm_gateway.storage.outbox import export_trace

pytestmark = pytest.mark.offline


@pytest.mark.parametrize("kind", ["public_file", "public_parent", "symlink"])
def test_invalid_trace_destination_preserves_pending_outbox(tmp_path, kind):
    engine, _, _ = configured(tmp_path)
    destination = tmp_path / "metadata.jsonl"
    if kind == "public_file":
        destination.write_text("")
        destination.chmod(0o644)
    elif kind == "public_parent":
        parent = tmp_path / "public"
        parent.mkdir(mode=0o755)
        destination = parent / "metadata.jsonl"
    else:
        target = tmp_path / "target"
        target.write_text("unchanged")
        destination.symlink_to(target)
    with pytest.raises((OSError, StoreError)):
        export_trace(engine.store, "project-a", destination)
    with engine.store.connection() as db:
        assert db.execute("SELECT count(*) FROM trace_outbox WHERE exported=0").fetchone()[0] > 0
    if kind == "public_parent":
        assert not destination.exists()
    elif kind == "symlink":
        assert target.read_text() == "unchanged"


def test_fifo_trace_sink_without_reader_is_rejected_with_bounded_wait(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    # A subprocess deadline makes a regression to blocking open fail safely.
    script = """import json, sys
from pathlib import Path
from llm_gateway.storage import StateStore, StoreError
from llm_gateway.storage.outbox import export_trace
try:
    export_trace(StateStore(Path(sys.argv[1])), 'synthetic', Path(sys.argv[2]))
except (OSError, StoreError):
    print(json.dumps({'blocked': True}))
else:
    print(json.dumps({'blocked': False}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(root), str(fifo)],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    assert json.loads(result.stdout) == {"blocked": True}
