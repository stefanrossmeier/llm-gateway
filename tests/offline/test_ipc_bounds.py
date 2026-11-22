"""Bounded IPC uses actual pipes/process groups, with no SDK/network/credentials."""

import os
import threading
import time
from pathlib import Path

import pytest

from llm_gateway.isolation import process as ipc
from llm_gateway.isolation.process import WorkerFailure, run_worker

pytestmark = pytest.mark.offline


def invoke(tmp_path, script, *, message=None, **options):
    source = tmp_path / "synthetic_worker.py"
    source.write_text(script)
    return run_worker(
        source,
        message if message is not None else {"public": True},
        state_root=tmp_path,
        environment=lambda private: {"PATH": "/usr/bin:/bin", "HOME": private},
        timeout=options.pop("timeout", 5),
        **options,
    )


def test_large_input_and_output_are_pumped_without_pipe_deadlock(tmp_path):
    value = '[{"quoted":"[ignored]"}]\\' * (16 * 1024)
    result = invoke(
        tmp_path,
        "import sys,json\nr=json.load(sys.stdin)\nprint(json.dumps(r))",
        message={"data": value},
    )
    assert result == {"data": value}
    assert not list(tmp_path.glob("worker-*"))


def test_continuous_output_is_killed_at_the_bound_without_buffering_the_whole_stream(tmp_path):
    start = time.monotonic()
    with pytest.raises(WorkerFailure, match="worker_process_lost") as error:
        invoke(
            tmp_path,
            "import sys,os\nsys.stdin.buffer.read()\nwhile True: os.write(1,b'x'*65536)",
            max_output=1024,
            timeout=4,
        )
    assert error.value.submitted is True and time.monotonic() - start < 3
    assert not list(tmp_path.glob("worker-*"))


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b'{"value":NaN}',
        b'{"value":1e9999}',
        b'{"value":-Infinity}',
        b"[]",
        b"\xff",
        b'{"value":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
    ],
)
def test_invalid_or_nonfinite_worker_protocol_retains_submission_uncertainty(tmp_path, body):
    script = "import sys\nsys.stdin.buffer.read()\nsys.stdout.buffer.write(" + repr(body) + ")"
    with pytest.raises(WorkerFailure, match="worker_protocol_invalid") as error:
        invoke(tmp_path, script)
    assert error.value.submitted is True


@pytest.mark.parametrize(
    "message,options,code",
    [
        ({"number": float("nan")}, {}, "worker_input_invalid"),
        ({"text": "\ud800"}, {}, "worker_input_invalid"),
        ({"text": "x" * 1000}, {"max_input": 10}, "worker_input_too_large"),
        ({"public": True}, {"timeout": 0}, "worker_deadline_exceeded"),
    ],
)
def test_invalid_or_expired_input_never_launches_a_worker(tmp_path, message, options, code):
    with pytest.raises(WorkerFailure, match=code) as error:
        invoke(
            tmp_path, "raise AssertionError('worker must not launch')", message=message, **options
        )
    assert error.value.submitted is False


def test_pre_cancelled_request_never_launches_a_worker(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(WorkerFailure, match="worker_cancelled") as error:
        invoke(tmp_path, "raise AssertionError('worker must not launch')", cancel=cancel)
    assert error.value.submitted is False


def test_selector_resource_failure_happens_before_launch_or_credential_resolution(
    tmp_path, monkeypatch
):
    def unavailable():
        raise OSError("synthetic descriptor exhaustion")

    monkeypatch.setattr(ipc.selectors, "DefaultSelector", unavailable)
    monkeypatch.setattr(
        ipc.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("A worker was launched without IPC ownership"),
    )
    with pytest.raises(OSError):
        invoke(tmp_path, "raise AssertionError('worker must not launch')")
    assert not list(tmp_path.glob("worker-*"))


def test_pipe_failure_after_submission_keeps_uncertainty_and_reaps_the_group(tmp_path, monkeypatch):
    launched, stdout_fds = [], []
    original_launch, original_read = ipc.subprocess.Popen, os.read

    def launch(*args, **kwargs):
        child = original_launch(*args, **kwargs)
        launched.append(child)
        stdout_fds.append(child.stdout.fileno())
        return child

    def read(fd, size):
        if stdout_fds and fd == stdout_fds[-1]:
            raise OSError("synthetic private failure text")
        return original_read(fd, size)

    monkeypatch.setattr(ipc.subprocess, "Popen", launch)
    monkeypatch.setattr(ipc.os, "read", read)
    with pytest.raises(WorkerFailure, match="worker_process_lost") as error:
        invoke(
            tmp_path,
            "import sys,time\nsys.stdin.buffer.read()\nprint('{}',flush=True)\ntime.sleep(30)",
        )
    assert error.value.submitted is True
    assert launched[0].poll() is not None
    assert "private failure" not in str(error.value)
    assert not list(tmp_path.glob("worker-*"))


def test_group_is_reaped_when_leader_exits_but_descendant_keeps_stdout_open(tmp_path):
    pid_file = tmp_path / "descendant.pid"
    script = (
        "import os,sys,time\n"
        "sys.stdin.buffer.read()\n"
        "pid=os.fork()\n"
        "if pid==0:\n"
        " time.sleep(30)\n"
        "else:\n"
        f" open({str(pid_file)!r},'w').write(str(pid))\n"
        " os._exit(0)\n"
    )
    with pytest.raises(WorkerFailure, match="worker_deadline_exceeded"):
        invoke(tmp_path, script, timeout=1)
    child = int(pid_file.read_text())
    # A reparented zombie has terminated; it may await the platform init reaper.
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        proc = Path(f"/proc/{child}/stat")
        if proc.exists() and proc.read_text().split(")", 1)[1].split()[0] == "Z":
            break
        time.sleep(0.02)
    else:
        pytest.fail("Owned descendant survived leader exit and cleanup")
