"""Synthetic reports verify fail-closed coverage admission, not code coverage itself."""

import copy
import json
import os
import runpy
from pathlib import Path

import pytest

pytestmark = pytest.mark.offline
GATE = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/check_coverage.py"))


@pytest.fixture
def evidence(tmp_path):
    source = tmp_path / "src/llm_gateway/sample.py"
    source.parent.mkdir(parents=True)
    source.write_text("def sample(flag):\n    return 1 if flag else 0\n")
    report = {
        "meta": {"branch_coverage": True},
        "files": {
            "src/llm_gateway/sample.py": {
                "summary": {
                    "num_branches": 10,
                    "covered_branches": 9,
                    "num_statements": 5,
                    "covered_lines": 5,
                    "missing_lines": 0,
                    "excluded_lines": 0,
                },
                "missing_branches": [[1, 2]],
                "excluded_lines": [],
            }
        },
    }
    path = tmp_path / "coverage.json"
    return tmp_path, source, path, report


def check(evidence, change=None):
    root, _, path, report = evidence
    data = copy.deepcopy(report)
    if change:
        change(data)
    path.write_text(json.dumps(data))
    return GATE["check"](path, root)


def test_every_authored_module_meets_threshold_without_exclusions(evidence):
    result = check(evidence)
    assert result == {
        "status": "passed",
        "authored_modules": 1,
        "minimum_required_branch_percent": 90,
        "minimum_observed_branch_percent": 90,
        "excluded_lines": 0,
    }


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "extra",
        "no_branch_mode",
        "excluded",
        "invalid_count",
        "boolean_count",
        "missing_arcs",
        "repeated_arcs",
        "below_threshold",
        "unexecuted",
        "bad_entry",
        "bad_summary",
    ],
)
def test_incomplete_or_weakened_reports_fail_admission(evidence, kind):
    def change(data):
        entry = data["files"]["src/llm_gateway/sample.py"]
        summary = entry["summary"]
        if kind == "missing":
            data["files"] = {}
        elif kind == "extra":
            data["files"]["stale/duplicate.py"] = entry
        elif kind == "no_branch_mode":
            data["meta"]["branch_coverage"] = False
        elif kind == "excluded":
            summary["excluded_lines"] = 1
        elif kind == "invalid_count":
            summary["covered_branches"] = 11
        elif kind == "boolean_count":
            summary["covered_branches"] = True
        elif kind == "missing_arcs":
            entry["missing_branches"] = []
        elif kind == "repeated_arcs":
            summary["covered_branches"] = 8
            entry["missing_branches"] *= 2
        elif kind == "below_threshold":
            summary["covered_branches"] = 8
            entry["missing_branches"].append([2, 3])
        elif kind == "unexecuted":
            summary.update(num_branches=0, covered_branches=0, covered_lines=0, missing_lines=5)
            entry["missing_branches"] = []
        elif kind == "bad_entry":
            data["files"]["src/llm_gateway/sample.py"] = None
        else:
            entry["summary"] = None

    with pytest.raises(SystemExit):
        check(evidence, change)


@pytest.mark.parametrize(
    "kind", ["symlink", "fifo", "oversize", "json", "duplicate_key", "wrong_root"]
)
def test_report_file_is_bounded_regular_unambiguous_json(evidence, kind):
    root, source, path, report = evidence
    if kind == "symlink":
        path.symlink_to(source)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "oversize":
        path.write_bytes(b" " * (GATE["LIMIT"] + 1))
    elif kind == "json":
        path.write_text("{")
    elif kind == "duplicate_key":
        path.write_text('{"meta":{},"meta":{"branch_coverage":true},"files":{}}')
    else:
        path.write_text("[]")
    with pytest.raises(SystemExit):
        GATE["check"](path, root)


def test_new_source_and_symlinked_source_cannot_disappear_from_inventory(evidence):
    root, source, path, report = evidence
    path.write_text(json.dumps(report))
    extra = source.parent / "new_module.py"
    extra.write_text("new_value = 1\n")
    with pytest.raises(SystemExit, match="inventory mismatch"):
        GATE["check"](path, root)
    extra.unlink()
    extra.symlink_to(source)
    with pytest.raises(SystemExit, match="inventory unavailable or invalid"):
        GATE["check"](path, root)


def test_empty_source_inventory_does_not_pass(tmp_path):
    path = tmp_path / "coverage.json"
    path.write_text('{"meta":{"branch_coverage":true},"files":{}}')
    with pytest.raises(SystemExit, match="inventory unavailable"):
        GATE["check"](path, tmp_path)
