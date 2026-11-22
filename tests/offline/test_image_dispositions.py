"""Exact, current per-finding evidence must not become a blanket CVE allowlist."""

import base64
import copy
import hashlib
import json
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.offline
ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 11, 15, tzinfo=UTC)


@pytest.fixture
def evidence(tmp_path):
    module = runpy.run_path(str(ROOT / "scripts/check_image_advisories.py"))
    root = tmp_path / "project"
    (root / "src/llm_gateway").mkdir(parents=True)
    (root / "src/llm_gateway/__init__.py").write_text('"""Synthetic package, never executed."""\n')
    files = [
        "Dockerfile",
        "pyproject.toml",
        "uv.lock",
        "deploy/compose.yaml",
        "deploy/install_security_updates.sh",
        "scripts/probe_native_profile.py",
        "docs/evidence/synthetic.md",
    ]
    reviewed = {}
    for relative in files:
        file = root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("SYNTHETIC-1 original scoped rationale")
        reviewed[relative] = hashlib.sha256(file.read_bytes()).hexdigest()
    profile = json.loads(
        (ROOT / "docs/evidence/native-profile-arm64-current-2026-11-11.json").read_text()
    )
    profile["sensitive_native_imports"] = []
    profile = {k: profile[k] for k in module["PROFILE_KEYS"] if k in profile}
    config = {
        "architecture": "arm64",
        "os": "linux",
        "rootfs": {"diff_ids": ["sha256:" + "1" * 64]},
        "config": {"User": "10001:10001", "Entrypoint": ["python", "-I"]},
    }
    raw = json.dumps(config).encode()
    target = {
        "config": base64.b64encode(raw).decode(),
        "imageID": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "architecture": "arm64",
        "os": "linux",
    }
    profile.update(
        authored_source_sha256=module["source_digest"](root),
        authored_source_files=1,
        image_runtime_identity_sha256=module["runtime_identity"](target),
    )
    policy = {k: [] for k in module["SCANNER_POLICY_KEYS"]}
    policy.update({"only-fixed": False, "only-notfixed": False, "ignore-wontfix": "", "distro": ""})
    configuration = policy | {"db": {"validate-by-hash-on-start": True, "validate-age": True}}
    component = {
        "namespace": "synthetic:fixture",
        "type": "deb",
        "name": "synthetic",
        "version": "1",
    }
    scan = {
        "source": {"type": "image", "target": target},
        "distro": {"name": "synthetic"},
        "descriptor": {
            "name": "grype",
            "version": "0.121.3",
            "configuration": configuration,
            "timestamp": NOW.isoformat(),
            "db": {"status": {"built": NOW.isoformat(), "valid": True}},
        },
        "matches": [
            {
                "vulnerability": {"id": "SYNTHETIC-1", "namespace": component["namespace"]},
                "artifact": {k: component[k] for k in ("type", "name", "version")},
            }
        ],
    }
    assessment = {
        "schema_version": 1,
        "release_applicability_approved": True,
        "reviewed_by": "synthetic offline fixture, no human approval",
        "reviewed_at": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": (NOW + timedelta(days=1)).isoformat(),
        "scanner_version": "0.121.3",
        "scanner_policy_sha256": module["digest"](policy),
        "distro": scan["distro"],
        "profiles": {"arm64": copy.deepcopy(profile)},
        "reviewed_files": reviewed,
        "findings": [
            {
                "id": "SYNTHETIC-1",
                "source_assessment": "synthetic.md",
                "disposition": "not_affected_restricted_profile",
                "components": [component],
            }
        ],
    }
    return module, root, scan, assessment, profile


def execute(evidence, tmp_path):
    module, root, scan, assessment, profile = evidence
    paths = []
    for name, value in [("scan", scan), ("assessment", assessment), ("profile", profile)]:
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(value))
        paths.append(path)
    module["check"](paths[0], assessment=paths[1], profile=paths[2], root=root, now=NOW)


def test_exact_review_passes_without_claiming_general_clean_scan(evidence, tmp_path, capsys):
    execute(evidence, tmp_path)
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "raw_matching_advisories": 1,
        "restricted_profile_dispositions_verified": 1,
        "general_clean_scan_claim": False,
    }


@pytest.mark.parametrize(
    "bad",
    [
        "unapproved",
        "expired",
        "future",
        "long_review",
        "no_reviewer",
        "scanner",
        "filter",
        "approved_filter",
        "hash_validation",
        "age_validation",
        "old_db",
        "invalid_db",
        "old_scan",
        "future_scan",
        "suppressed",
        "distro",
        "not_image",
        "profile",
        "source",
        "build",
        "rationale",
        "disposition",
        "duplicate",
        "config_digest",
        "filesystem",
        "root_uid",
        "capabilities",
        "privileges",
        "writable",
        "inference",
        "new_cve",
        "new_version",
        "new_namespace",
        "new_package",
        "new_type",
    ],
)
def test_changed_missing_stale_or_weaker_evidence_fails_closed(evidence, tmp_path, bad, capsys):
    module, root, scan, approval, profile = evidence
    if bad == "unapproved":
        approval["release_applicability_approved"] = False
    elif bad == "expired":
        approval["expires_at"] = NOW.isoformat()
    elif bad == "future":
        approval["reviewed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif bad == "long_review":
        approval["expires_at"] = (NOW + timedelta(days=15)).isoformat()
    elif bad == "no_reviewer":
        approval["reviewed_by"] = ""
    elif bad == "scanner":
        scan["descriptor"]["version"] = "unreviewed"
    elif bad == "filter":
        scan["descriptor"]["configuration"]["ignore"].append({"namespace": "*"})
    elif bad == "approved_filter":
        scan["descriptor"]["configuration"]["only-fixed"] = True
        approval["scanner_policy_sha256"] = module["digest"](
            module["scanner_policy"](scan["descriptor"]["configuration"])
        )
    elif bad.endswith("validation"):
        scan["descriptor"]["configuration"]["db"][
            "validate-by-hash-on-start" if bad == "hash_validation" else "validate-age"
        ] = False
    elif bad == "old_db":
        scan["descriptor"]["db"]["status"]["built"] = (NOW - timedelta(hours=49)).isoformat()
    elif bad == "invalid_db":
        scan["descriptor"]["db"]["status"]["valid"] = False
    elif bad == "old_scan":
        scan["descriptor"]["timestamp"] = (NOW - timedelta(hours=25)).isoformat()
    elif bad == "future_scan":
        scan["descriptor"]["timestamp"] = (NOW + timedelta(minutes=6)).isoformat()
    elif bad == "suppressed":
        scan["ignoredMatches"] = [{"synthetic": "hidden"}]
    elif bad == "distro":
        scan["distro"] = {"name": "other"}
    elif bad == "not_image":
        scan["source"]["type"] = "directory"
    elif bad == "profile":
        profile["native_objects_referencing_libstdcpp"] = []
    elif bad == "source":
        (root / "src/llm_gateway/__init__.py").write_text("changed")
    elif bad == "build":
        (root / "Dockerfile").write_text("changed")
    elif bad == "rationale":
        approval["reviewed_files"].pop("docs/evidence/synthetic.md")
    elif bad == "disposition":
        approval["findings"][0]["disposition"] = "ignore_all_low_severity"
    elif bad == "duplicate":
        approval["findings"].append(copy.deepcopy(approval["findings"][0]))
    elif bad == "config_digest":
        scan["source"]["target"]["imageID"] = "sha256:" + "0" * 64
    elif bad == "filesystem":
        target = scan["source"]["target"]
        config = json.loads(base64.b64decode(target["config"]))
        config["rootfs"]["diff_ids"] = ["sha256:" + "2" * 64]
        raw = json.dumps(config).encode()
        target.update(
            config=base64.b64encode(raw).decode(),
            imageID="sha256:" + hashlib.sha256(raw).hexdigest(),
        )
    elif bad in {"root_uid", "capabilities", "privileges", "writable", "inference"}:
        field, value = {
            "root_uid": ("effective_uid", 0),
            "capabilities": ("effective_capabilities", "1"),
            "privileges": ("no_new_privileges", False),
            "writable": ("read_only_root", False),
            "inference": ("inference_performed", True),
        }[bad]
        profile[field] = value
        approval["profiles"]["arm64"][field] = value
    else:
        match = scan["matches"][0]
        if bad == "new_cve":
            match["vulnerability"]["id"] = "SYNTHETIC-2"
        elif bad == "new_namespace":
            match["vulnerability"]["namespace"] = "other"
        else:
            match["artifact"][
                {"new_version": "version", "new_package": "name", "new_type": "type"}[bad]
            ] = "other"
    with pytest.raises(SystemExit, match="evidence rejected"):
        execute(evidence, tmp_path)
    assert capsys.readouterr().out == ""


def test_assessment_requires_runtime_evidence_and_invalid_json_is_sanitized(tmp_path):
    module = runpy.run_path(str(ROOT / "scripts/check_image_advisories.py"))
    path = tmp_path / "scan.json"
    path.write_text('{"descriptor":{"version":"fixture"},"distro":{"name":"fixture"},"matches":[]}')
    with pytest.raises(SystemExit, match="both assessment"):
        module["check"](path, assessment=path)
    path.write_text('{"private-canary":invalid}')
    with pytest.raises(SystemExit) as error:
        module["check"](path)
    assert "private-canary" not in str(error.value)


def test_default_gate_requires_complete_raw_scan_and_rejects_all_matches(
    evidence, tmp_path, capsys
):
    module, _, scan, _, _ = evidence
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(scan))
    with pytest.raises(SystemExit) as error:
        module["check"](path, now=NOW)
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out)["unresolved_image_advisories"] == 1
    scan["matches"] = []
    path.write_text(json.dumps(scan))
    module["check"](path, now=NOW)
    assert "no unresolved matching" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversize"])
def test_gate_reads_only_bounded_regular_evidence_files(tmp_path, kind):
    import os

    module = runpy.run_path(str(ROOT / "scripts/check_image_advisories.py"))
    path = tmp_path / "input"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("private-canary")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        with path.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(SystemExit) as error:
        module["check"](path)
    assert "private-canary" not in str(error.value)
