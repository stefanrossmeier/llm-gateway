import json
import runpy
from pathlib import Path

import pytest

pytestmark = pytest.mark.offline
ROOT = Path(__file__).resolve().parents[2]


def test_lock_review_binds_exact_bytes_and_rejects_new_findings(tmp_path):
    check = runpy.run_path(str(ROOT / "scripts/check_lock_approval.py"))["check"]
    evidence = tmp_path / "docs/evidence"
    evidence.mkdir(parents=True)
    review_name = "dependency-review-2026-11-25.json"
    (tmp_path / "uv.lock").write_bytes((ROOT / "uv.lock").read_bytes())
    original = json.loads((ROOT / "docs/evidence" / review_name).read_text())
    (evidence / review_name).write_text(json.dumps(original))
    check(tmp_path)
    (tmp_path / "uv.lock").write_bytes((ROOT / "uv.lock").read_bytes() + b"\n# changed\n")
    with pytest.raises(SystemExit, match="mismatched"):
        check(tmp_path)
    (tmp_path / "uv.lock").write_bytes((ROOT / "uv.lock").read_bytes())
    original["automated_findings"] = [{"synthetic": "unresolved"}]
    (evidence / review_name).write_text(json.dumps(original))
    with pytest.raises(SystemExit, match="mismatched"):
        check(tmp_path)


def test_image_gate_rejects_incomplete_scan_even_when_matches_are_empty(tmp_path):
    check = runpy.run_path(str(ROOT / "scripts/check_image_advisories.py"))["check"]
    path = tmp_path / "scan.json"
    path.write_text("{}")
    with pytest.raises(SystemExit, match="incomplete"):
        check(path)
    path.write_text(
        json.dumps(
            {"descriptor": {"version": "fixture"}, "distro": {"name": "fixture"}, "matches": []}
        )
    )
    with pytest.raises(SystemExit, match="missing or invalid evidence"):
        check(path)
