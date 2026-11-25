"""A native probe cannot approve stale source or weaker process restrictions."""

import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.offline
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "bad", [None, "source", "uid", "capabilities", "privileges", "writable", "failed", "missing"]
)
def test_probe_rejects_changed_source_or_runtime_and_always_cleans_own_container(
    monkeypatch, capsys, bad
):
    module = runpy.run_path(str(ROOT / "scripts/probe_native_profile.py"))
    root = ROOT / "src/llm_gateway"
    authored = [
        [str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest()]
        for path in sorted([*root.rglob("*.py"), *root.glob("config/providers/*.json")])
    ]
    digest = hashlib.sha256(
        json.dumps(authored, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    report = {
        "authored_source_sha256": digest,
        "effective_uid": 10001,
        "effective_capabilities": "0000000000000000",
        "no_new_privileges": True,
        "read_only_root": True,
    }
    changes = {
        "source": ("authored_source_sha256", "0" * 64),
        "uid": ("effective_uid", 0),
        "capabilities": ("effective_capabilities", "0000000000000001"),
        "privileges": ("no_new_privileges", False),
        "writable": ("read_only_root", False),
    }
    if bad in changes:
        key, value = changes[bad]
        report[key] = value
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[1] == "rm":
            return SimpleNamespace(returncode=0)
        assert "--pull=never" in args and args[args.index("--network") + 1] == "none"
        assert "--read-only" in args and "no-new-privileges" in args
        return SimpleNamespace(
            returncode=1 if bad == "failed" else 0,
            stdout="" if bad == "missing" else "NATIVE_PROFILE " + json.dumps(report),
        )

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *a, **k: json.dumps(
            [
                {
                    "Image": "sha256:" + "1" * 64,
                    "Architecture": "arm64",
                    "Os": "linux",
                    "RootFS": {"Layers": ["sha256:" + "3" * 64]},
                    "Config": {"User": "10001:10001"},
                    "ImageManifestDescriptor": {"digest": "sha256:" + "2" * 64},
                }
            ]
        ),
    )
    monkeypatch.setattr(sys, "argv", ["probe", "--image", "synthetic-local-image"])
    if bad is None:
        module["main"]()
        assert json.loads(capsys.readouterr().out)["applicability_approved"] is False
    else:
        with pytest.raises(SystemExit):
            module["main"]()
        assert capsys.readouterr().out == ""
    name = calls[0][calls[0].index("--name") + 1]
    assert name.startswith("gateway-native-")
    assert calls[-1] == ["docker", "rm", "-f", name]
