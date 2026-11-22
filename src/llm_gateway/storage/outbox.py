"""At-least-once metadata export; consumers deduplicate by durable event ID."""

import json
import os
import stat
from pathlib import Path

from .sqlite import StoreError, verify_private_ancestors


def export_trace(store, project, destination):
    destination = Path(destination)
    parent = destination.parent.resolve()
    verify_private_ancestors(parent)
    # Keep subsequent opens on the verified canonical parent. A FIFO must not
    # block before the descriptor's regular-file check can reject it.
    destination = parent / destination.name
    fd = os.open(
        destination,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise StoreError("trace_destination_not_private")
        with store.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM trace_outbox WHERE project_id=? AND exported=0 ORDER BY id "
                "LIMIT 1000",
                (project,),
            ).fetchall()
        with os.fdopen(fd, "a", closefd=False) as stream:
            for row in rows:
                stream.write(
                    json.dumps(
                        dict(
                            event_id=row["id"],
                            project_id=project,
                            request_id=row["request_id"],
                            **json.loads(row["event_json"]),
                        ),
                        allow_nan=False,
                    )
                    + "\n"
                )
            stream.flush()
            os.fsync(fd)
        with store.transaction() as connection:
            connection.executemany(
                "UPDATE trace_outbox SET exported=1 WHERE id=? AND project_id=?",
                [(r["id"], project) for r in rows],
            )
        return len(rows)
    finally:
        os.close(fd)
