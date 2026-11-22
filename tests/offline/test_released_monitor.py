"""Released monitoring uses current tools with the manifest's static source tree."""

import base64
import copy
import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from test_image_dispositions import NOW
from test_release_assets import fixture as release_fixture

pytestmark = pytest.mark.offline
pytest_plugins = ["test_image_dispositions"]
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/security.yml"


def steps():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["released"]["steps"]


def script(name):
    step = next(s for s in steps() if s.get("name") == name)
    return step["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


@pytest.fixture
def workspace(tmp_path, evidence, monkeypatch):
    gate, original, scan, approval, profile = evidence
    current = tmp_path / "current"
    current.mkdir()
    source = current / "released-source"
    shutil.copytree(original, source)
    environment = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }

    def git(*args):
        return subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=Synthetic offline fixture",
                "-c",
                "user.email=fixture@example.invalid",
                *args,
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout.strip()

    git("init", "--quiet")
    git("add", ".")
    git("commit", "--quiet", "-m", "Synthetic static release source, never executed")
    module, _, assets, data = release_fixture(tmp_path)
    data.update(
        source_commit=git("rev-parse", "HEAD"), authored_source_sha256=gate["source_digest"](source)
    )
    manifest = current / "release/0.1.0a3.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps(data))
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    tools = {"scripts/check_release_assets.py": module, "scripts/check_image_advisories.py": gate}
    monkeypatch.setattr(runpy, "run_path", lambda path: tools[str(path)])
    monkeypatch.chdir(current)
    return current, source, manifest, output, assets, data, gate, scan, approval, profile


def test_manifest_output_and_source_verification_ignore_main_drift(workspace):
    current, source, _, output, _, data, gate, *_ = workspace
    # Main has newer runtime code; the released snapshot is deliberately unchanged.
    (current / "src/llm_gateway").mkdir(parents=True)
    (current / "src/llm_gateway/__init__.py").write_text("new main implementation")
    assert gate["source_digest"](current) != data["authored_source_sha256"]
    exec(script("Read trusted immutable release source identity"), {})
    assert output.read_text() == f"commit={data['source_commit']}\n"
    exec(script("Verify released commit and authored source identity before review"), {})
    checkout = next(s for s in steps() if s.get("name", "").startswith("Check out exact"))
    assert re.fullmatch(r"actions/checkout@[a-f0-9]{40}", checkout["uses"])
    assert checkout["with"] == {
        "ref": "${{ steps.release.outputs.commit }}",
        "path": "released-source",
        "persist-credentials": False,
    }
    audit = next(s for s in steps() if s.get("name", "").startswith("Fresh exact-lock"))
    assert audit["run"].startswith("python scripts/audit_lock.py --lock released-source/uv.lock ")


@pytest.mark.parametrize("bad", ["noncommit", "wrong_commit", "source_changed", "missing_source"])
def test_untrusted_or_mismatched_source_fails_before_target_execution(workspace, bad):
    _, source, manifest, output, _, data, *_ = workspace
    if bad == "noncommit":
        data["source_commit"] = "main\nmalicious_output=true"
        manifest.write_text(json.dumps(data))
        with pytest.raises(ValueError):
            exec(script("Read trusted immutable release source identity"), {})
        assert not output.exists()
        return
    if bad == "wrong_commit":
        data["source_commit"] = "f" * 40
        manifest.write_text(json.dumps(data))
    elif bad == "source_changed":
        (source / "src/llm_gateway/__init__.py").write_text("changed source")
    else:
        shutil.rmtree(source)
    with pytest.raises((SystemExit, subprocess.CalledProcessError)):
        exec(script("Verify released commit and authored source identity before review"), {})


@pytest.mark.parametrize("failure_mode", ["none", "new_finding", "scanner_timeout"])
def test_current_gate_checks_both_archives_against_release_source_and_rejects_new_findings(
    workspace, monkeypatch, capsys, failure_mode
):
    current, source, _, _, assets, data, gate, original_scan, approval, original_profile = workspace
    scans = {}
    for arch, files in data["platforms"].items():
        scan = copy.deepcopy(original_scan)
        target = scan["source"]["target"]
        config = json.loads(base64.b64decode(target["config"]))
        config["architecture"] = arch
        raw = json.dumps(config).encode()
        target.update(
            architecture=arch,
            config=base64.b64encode(raw).decode(),
            imageID="sha256:" + hashlib.sha256(raw).hexdigest(),
        )
        profile = original_profile | {
            "architecture": arch,
            "image_runtime_identity_sha256": gate["runtime_identity"](target),
        }
        approval["profiles"][arch] = profile
        (assets / files["profile"]).write_text(json.dumps(profile))
        if failure_mode == "new_finding":
            scan["matches"][0]["vulnerability"]["id"] = "UNREVIEWED-NEW"
        scans[f"released-{arch}-advisories.json"] = scan
    review = current / "docs/evidence/container-candidate-assessment-2026-11-22.json"
    review.parent.mkdir(parents=True)
    review.write_text(json.dumps(approval))
    monkeypatch.setenv("RELEASE_ASSETS", str(assets))
    checked = []
    real = gate["check"]

    def check(path, **kwargs):
        assert kwargs["root"] == source.resolve()
        assert kwargs["assessment"] == Path(
            "docs/evidence/container-candidate-assessment-2026-11-22.json"
        )
        checked.append(path.name)
        real(path, **kwargs, now=NOW)  # Synthetic time belongs only to this offline fixture.

    gate["check"] = check

    def scan(args, **kwargs):
        assert args[:4] == ["docker", "run", "--rm", "--read-only"]
        assert "docker-archive:/image.tar" in args
        assert not any("docker.sock" in value for value in args)
        name = Path(args[args.index("--file") + 1]).name
        if failure_mode == "scanner_timeout" and name == "released-arm64-advisories.json":
            raise subprocess.TimeoutExpired("synthetic-scanner", 1)
        (current / "reports" / name).write_text(json.dumps(scans[name]))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", scan)
    block = script("Scan both exact archives and enforce current scoped dispositions")
    if failure_mode != "none":
        with pytest.raises(SystemExit, match="Released image review rejected"):
            exec(block, {})
        assert (current / "reports/released-amd64-advisories.json").is_file()
        assert (
            "unreviewed component/advisory" if failure_mode == "new_finding" else "scanner_failed"
        ) in capsys.readouterr().out
    else:
        exec(block, {})
    assert checked == (
        ["released-amd64-advisories.json"]
        if failure_mode == "scanner_timeout"
        else ["released-arm64-advisories.json", "released-amd64-advisories.json"]
    )
