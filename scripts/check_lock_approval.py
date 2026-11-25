"""Fail before installation when the frozen graph lacks matching review evidence."""

import hashlib
import json
import tomllib
from pathlib import Path


def check(root):
    raw = (root / "uv.lock").read_bytes()
    report = json.loads((root / "docs/evidence/dependency-review-2026-11-25.json").read_text())
    expected = {
        (p["name"], p["version"])
        for p in tomllib.loads(raw.decode())["package"]
        if "registry" in p["source"]
    }
    reviewed = {(p["name"], p["version"]) for p in report["packages"]}
    if (
        report["lock_sha256"] != hashlib.sha256(raw).hexdigest()
        or report["automated_findings"]
        or expected != reviewed
        or report["status"] != "no_automated_findings"
    ):
        raise SystemExit("Frozen dependency approval missing or mismatched; review before install")
    print("Frozen graph matches reviewed artifact/advisory evidence")


if __name__ == "__main__":
    check(Path(__file__).resolve().parents[1])
