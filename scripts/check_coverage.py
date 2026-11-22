"""Require branch coverage for every authored module, without exclusions."""

import argparse
import json
import os
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 8 * 1024 * 1024


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate coverage key")
        result[key] = value
    return result


def read_report(path):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
            raise ValueError()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError()
        return json.loads(raw, object_pairs_hook=unique)
    except (OSError, ValueError):
        raise SystemExit("coverage report unavailable or invalid") from None
    finally:
        if fd is not None:
            os.close(fd)


def check(path, root=ROOT):
    report = read_report(path)
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("meta"), dict)
        or report["meta"].get("branch_coverage") is not True
        or not isinstance(report.get("files"), dict)
    ):
        raise SystemExit("branch coverage evidence required")
    sources = list((root / "src/llm_gateway").rglob("*.py"))
    if not sources or any(not path.is_file() or path.is_symlink() for path in sources):
        raise SystemExit("authored source inventory unavailable or invalid")
    expected = {path.relative_to(root).as_posix() for path in sources}
    if set(report["files"]) != expected:
        raise SystemExit("coverage source inventory mismatch; use a clean frozen environment")
    failed, fractions = [], []
    for name in sorted(expected):
        entry = report["files"][name]
        summary = entry.get("summary", {}) if isinstance(entry, dict) else {}
        fields = (
            "num_branches",
            "covered_branches",
            "num_statements",
            "covered_lines",
            "missing_lines",
            "excluded_lines",
        )
        if not isinstance(summary, dict) or any(
            type(summary.get(key)) is not int or summary[key] < 0 for key in fields
        ):
            raise SystemExit("coverage summary invalid: " + name)
        total, covered = summary["num_branches"], summary["covered_branches"]
        missing = entry.get("missing_branches")
        if (
            covered > total
            or summary["covered_lines"] + summary["missing_lines"] != summary["num_statements"]
            or summary["excluded_lines"] != 0
            or entry.get("excluded_lines") != []
            or not isinstance(missing, list)
            or any(
                not isinstance(pair, list)
                or len(pair) != 2
                or any(type(value) is not int for value in pair)
                for pair in missing
            )
            or len({tuple(pair) for pair in missing}) != len(missing)
            or len(missing) != total - covered
        ):
            raise SystemExit("coverage counts/exclusions invalid: " + name)
        if summary["num_statements"] and not summary["covered_lines"]:
            failed.append(name + ": never executed")
        elif covered * 100 < total * 90:
            failed.append(f"{name}: {covered}/{total} branches (<90%)")
        fractions.append(covered / total if total else 1)
    if failed:
        raise SystemExit("coverage gate failed:\n" + "\n".join(failed))
    return {
        "status": "passed",
        "authored_modules": len(expected),
        "minimum_required_branch_percent": 90,
        "minimum_observed_branch_percent": round(min(fractions) * 100, 2),
        "excluded_lines": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.report)))


if __name__ == "__main__":
    main()
