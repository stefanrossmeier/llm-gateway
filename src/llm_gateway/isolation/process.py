"""Bounded private JSON IPC with process-group cancellation and reaping."""

import json
import math
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from decimal import Decimal


class WorkerFailure(Exception):
    def __init__(self, code, *, submitted=False):
        self.code, self.submitted = code, submitted
        super().__init__(code)


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError()
    return result


def _check_depth(text):
    # Scan before the C JSON decoder: another imported library may have raised
    # Python's recursion limit. Brackets inside escaped JSON strings do not count.
    depth, quoted, escaped = 0, False, False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > 96:
                raise ValueError()
        elif character in "]}":
            depth -= 1


def run_worker(
    script,
    message,
    *,
    state_root,
    environment,
    timeout,
    cancel=None,
    max_input=40 * 1024 * 1024,
    max_output=20 * 1024 * 1024,
):
    if os.name != "posix":
        raise WorkerFailure("worker_platform_unsupported")
    deadline = time.monotonic() + timeout
    if cancel is not None and cancel.is_set():
        raise WorkerFailure("worker_cancelled")
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            default=lambda value: float(value) if isinstance(value, Decimal) else str(value),
        ).encode()
    except (ValueError, UnicodeError, TypeError):
        raise WorkerFailure("worker_input_invalid") from None
    if len(payload) > max_input:
        raise WorkerFailure("worker_input_too_large")
    if time.monotonic() >= deadline:
        raise WorkerFailure("worker_deadline_exceeded")
    with (
        selectors.DefaultSelector() as selector,
        tempfile.TemporaryDirectory(prefix="worker-", dir=state_root) as private,
    ):
        process = subprocess.Popen(
            [sys.executable, "-I", str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment(private),
            cwd=private,
            start_new_session=True,
        )
        submitted = False
        try:
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            pending = memoryview(payload)
            output = bytearray()
            output_closed = False
            while True:
                if cancel is not None and cancel.is_set():
                    raise WorkerFailure("worker_cancelled", submitted=submitted)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerFailure("worker_deadline_exceeded", submitted=submitted)
                if output_closed and process.poll() is not None:
                    break
                if not selector.get_map():
                    try:
                        process.wait(timeout=min(0.05, remaining))
                    except subprocess.TimeoutExpired:
                        pass
                    continue
                for key, _ in selector.select(timeout=min(0.05, remaining)):
                    if key.fileobj is process.stdin:
                        try:
                            count = os.write(process.stdin.fileno(), pending[:65536])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            raise WorkerFailure(
                                "worker_process_lost", submitted=submitted
                            ) from None
                        submitted = submitted or count > 0
                        pending = pending[count:]
                        if not pending:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        try:
                            chunk = os.read(
                                process.stdout.fileno(), min(65536, max_output - len(output) + 1)
                            )
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(process.stdout)
                            output_closed = True
                        else:
                            output.extend(chunk)
                            if len(output) > max_output:
                                raise WorkerFailure("worker_process_lost", submitted=submitted)
            if process.returncode != 0 or pending:
                raise WorkerFailure("worker_process_lost", submitted=submitted)
            try:
                text = output.decode("utf-8")
                _check_depth(text)
                response = json.loads(
                    text,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                    parse_float=_finite_float,
                )
                if not isinstance(response, dict):
                    raise ValueError()
                return response
            except (ValueError, UnicodeError, RecursionError):
                raise WorkerFailure("worker_protocol_invalid", submitted=submitted) from None
        except OSError:
            raise WorkerFailure("worker_process_lost", submitted=submitted) from None
        finally:
            # The group can outlive its leader (for example inherited stdout).
            # Reap owned descendants even when the leader has already exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for stream in (process.stdin, process.stdout):
                stream.close()
            process.wait()
