"""Bounded local disclosure checks; never publish or print matched secret values."""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    rb"sk-or-v1-[0-9a-fA-F]{64}",
    rb"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{48,}",
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    rb"(?:ghp_|github_pat_)[A-Za-z0-9_]{36,}",
    rb"AKIA[A-Z0-9]{16}",
)


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def scan():
    required = [
        "README.md",
        "QUICKSTART.md",
        "LICENSE",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "SUPPORT.md",
        "CODE_OF_CONDUCT.md",
        "CHANGELOG.md",
        "THIRD_PARTY_NOTICES.md",
        "docs/README.md",
        "docs/INSTALLATION.md",
        "docs/CONFIGURATION.md",
        "docs/API.md",
        "docs/CLIENT.md",
        "docs/TOKENS.md",
        "docs/OPERATIONS.md",
        "docs/TESTING.md",
        "docs/LIVE_TEST_PLANS.md",
        "docs/RELEASING.md",
        "docs/PUBLICATION_REVIEW.md",
    ]
    if any(not (ROOT / name).is_file() for name in required):
        raise SystemExit("Required publication documentation is missing")
    if "Copyright (c) 2026 Stefan Rossmeier" not in (ROOT / "LICENSE").read_text():
        raise SystemExit("License attribution does not match the owner")
    objects = git("rev-list", "--objects", "--all").splitlines()
    identifiers = [line.split(b" ", 1)[0] for line in objects]
    inventory = subprocess.check_output(
        ["git", "-C", str(ROOT), "cat-file", "--batch-check"],
        input=b"\n".join(identifiers) + b"\n",
    )
    blobs = []
    total = 0
    for line in inventory.splitlines():
        oid, kind, size = line.split()
        if kind == b"blob":
            total += int(size)
            if total > 256 * 1024 * 1024 or int(size) > 32 * 1024 * 1024:
                raise SystemExit("History exceeds bounded scan; review separately")
            blobs.append(oid)
    findings = []
    for oid in blobs:
        data = git("cat-file", "blob", oid.decode())
        if any(re.search(pattern, data) for pattern in PATTERNS):
            findings.append(oid.decode())  # Never include the matched value.
    tracked = git("ls-files", "-z").split(b"\0")
    private_tracked = [
        name.decode()
        for name in tracked
        if name.startswith((b".gateway-state/", b".env", b".venv/"))
        or name.endswith((b".sqlite", b".sqlite3", b".token", b".pem"))
    ]
    working_findings = []
    for name in tracked:
        if not name:
            continue
        path = ROOT / name.decode()
        if path.is_file() and any(re.search(pattern, path.read_bytes()) for pattern in PATTERNS):
            working_findings.append(name.decode())
    result = {
        "head": git("rev-parse", "HEAD").decode().strip(),
        "history_blobs_scanned": len(blobs),
        "history_bytes_scanned": total,
        "high_confidence_secret_blob_ids": findings,
        "private_state_tracked_paths": private_tracked,
        "high_confidence_secret_working_paths": working_findings,
        "required_documents_present": len(required),
        "scope": "limited_secret_patterns_and_tracked_paths",
    }
    print(json.dumps(result, indent=2))
    if findings or private_tracked or working_findings:
        raise SystemExit(1)


if __name__ == "__main__":
    scan()
