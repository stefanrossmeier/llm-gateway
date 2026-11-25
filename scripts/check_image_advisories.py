"""Fail closed on raw findings unless exact, current profile evidence disposes them."""

import argparse
import base64
import hashlib
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNER_POLICY_KEYS = (
    "distro",
    "only-fixed",
    "only-notfixed",
    "ignore-wontfix",
    "ignore",
    "exclude",
    "search",
    "match",
    "externalSources",
    "by-cve",
    "show-suppressed",
    "include-matcher-suppressions",
    "vex-documents",
    "vex-add",
    "match-upstream-kernel-headers",
)
PROFILE_KEYS = (
    "authored_source_sha256",
    "authored_source_files",
    "image_runtime_identity_sha256",
    "architecture",
    "python_version",
    "openssl_version",
    "tls_cipher_names",
    "sensitive_native_imports",
    "loaded_libstdcpp",
    "native_objects_referencing_libstdcpp",
    "source_fixture_sha256",
    "adapter_calls_over_mock_http",
    "effective_uid",
    "effective_capabilities",
    "no_new_privileges",
    "read_only_root",
    "inference_performed",
    "applicability_approved",
)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        limit = 16 * 1024 * 1024
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("invalid evidence file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("oversized evidence")
        return json.loads(raw)
    finally:
        os.close(fd)


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("naive evidence time")
    return result


def reject(code):
    raise SystemExit("Image advisory evidence rejected: " + code)


def scanner_policy(configuration):
    return {key: configuration[key] for key in SCANNER_POLICY_KEYS}


def source_digest(root):
    package = root / "src/llm_gateway"
    rows = [
        [str(path.relative_to(package)), hashlib.sha256(path.read_bytes()).hexdigest()]
        for path in sorted([*package.rglob("*.py"), *package.glob("config/providers/*.json")])
    ]
    if not rows:
        raise ValueError("authored source absent")
    return hashlib.sha256(
        json.dumps(rows, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def runtime_identity(target):
    raw = base64.b64decode(target["config"], validate=True)
    config = json.loads(raw)
    if hashlib.sha256(raw).hexdigest() != target["imageID"].removeprefix("sha256:"):
        reject("scan configuration digest mismatch")
    if config["architecture"] != target["architecture"]:
        reject("scan architecture mismatch")
    return digest(
        {
            "architecture": config["architecture"],
            "os": config["os"],
            "rootfs_diff_ids": config["rootfs"]["diff_ids"],
            "config": config["config"],
        }
    )


def validate_scan(data, now):
    descriptor = data["descriptor"]
    configuration = descriptor["configuration"]
    if descriptor["name"] != "grype" or descriptor["version"] != "0.121.3":
        reject("scanner changed")
    if (
        configuration["only-fixed"] is not False
        or configuration["only-notfixed"] is not False
        or configuration["ignore-wontfix"] != ""
        or configuration["distro"] != ""
        or configuration["exclude"]
        or configuration["vex-documents"]
        or configuration["vex-add"]
    ):
        reject("raw scanner filtering is forbidden")
    header_defaults = {
        ("kernel-headers", "rpm"),
        ("linux(-.*)?-headers-.*", "deb"),
        ("linux-libc-dev", "deb"),
        ("linux-kbuild-.*", "deb"),
    }
    for rule in configuration["ignore"]:
        package = rule["package"]
        if (
            any(rule.get(key) for key in ("namespace", "vulnerability", "fix-state", "vex-status"))
            or (package["name"], package["type"]) not in header_defaults
            or rule["match-type"] != "exact-indirect-match"
        ):
            reject("custom raw scanner suppression is forbidden")
    if (
        configuration["db"]["validate-by-hash-on-start"] is not True
        or configuration["db"]["validate-age"] is not True
    ):
        reject("scanner database validation disabled")
    database = descriptor["db"]["status"]
    built, scanned = instant(database["built"]), instant(descriptor["timestamp"])
    if (
        database["valid"] is not True
        or not now - timedelta(hours=48) <= built <= now + timedelta(minutes=5)
        or not now - timedelta(hours=24) <= scanned <= now + timedelta(minutes=5)
    ):
        reject("scan/database is stale or incomplete")
    if data.get("ignoredMatches") or data.get("suppressedMatches"):
        reject("raw matches were suppressed")
    if data["source"]["type"] != "image":
        reject("scan is not an image")
    target = data["source"]["target"]
    if target["architecture"] not in {"arm64", "amd64"} or target["os"] != "linux":
        reject("unsupported image platform")
    runtime_identity(target)


def reviewed(data, assessment, profile, *, root, now):
    if (
        assessment.get("schema_version") != 1
        or assessment.get("release_applicability_approved") is not True
    ):
        reject("review is not approved")
    start, end = instant(assessment["reviewed_at"]), instant(assessment["expires_at"])
    if not start <= now < end or end - start > timedelta(days=14):
        reject("review expired or invalid")
    if not assessment.get("reviewed_by"):
        reject("review identity missing")
    descriptor = data["descriptor"]
    configuration = descriptor["configuration"]
    if descriptor["version"] != assessment["scanner_version"]:
        reject("scanner changed")
    if digest(scanner_policy(configuration)) != assessment["scanner_policy_sha256"]:
        reject("scanner filtering changed")
    if data["distro"] != assessment["distro"]:
        reject("distribution changed")
    target = data["source"]["target"]
    expected = assessment["profiles"][target["architecture"]]
    if any(profile[key] != expected[key] for key in PROFILE_KEYS):
        reject("runtime/native profile changed")
    if profile["authored_source_sha256"] != source_digest(root):
        reject("authored source changed")
    if runtime_identity(target) != profile["image_runtime_identity_sha256"]:
        reject("scan/runtime image mismatch")
    if (
        profile["sensitive_native_imports"] != []
        or profile["effective_uid"] != 10001
        or int(profile["effective_capabilities"], 16) != 0
        or profile["no_new_privileges"] is not True
        or profile["read_only_root"] is not True
        or profile["inference_performed"] is not False
        or profile["adapter_calls_over_mock_http"] != 8
    ):
        reject("runtime restrictions missing")
    approved_files = assessment["reviewed_files"]
    required = {
        "Dockerfile",
        "pyproject.toml",
        "uv.lock",
        "deploy/compose.yaml",
        "deploy/install_security_updates.sh",
        "scripts/probe_native_profile.py",
    }
    if not required.issubset(approved_files):
        reject("build/deployment evidence missing")
    for relative, expected_hash in approved_files.items():
        path = (root / relative).resolve()
        if (
            not path.is_relative_to(root.resolve())
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash
        ):
            reject("reviewed build/source assessment changed")
    allowed = set()
    for finding in assessment["findings"]:
        document = "docs/evidence/" + finding["source_assessment"]
        if (
            finding["disposition"] != "not_affected_restricted_profile"
            or document not in approved_files
        ):
            reject("finding has no scoped disposition")
        if finding["id"] not in (root / document).read_text():
            reject("finding source rationale missing")
        for component in finding["components"]:
            key = (
                component["namespace"],
                component["type"],
                component["name"],
                component["version"],
                finding["id"],
            )
            if key in allowed:
                reject("duplicate disposition")
            allowed.add(key)
    for match in data["matches"]:
        item, vulnerability = match["artifact"], match["vulnerability"]
        key = (
            vulnerability["namespace"],
            item["type"],
            item["name"],
            item["version"],
            vulnerability["id"],
        )
        if key not in allowed:
            reject("unreviewed component/advisory")


def check(path, *, assessment=None, profile=None, root=ROOT, now=None):
    try:
        data = read_json(path)
        if not data.get("descriptor", {}).get("version") or not data.get("distro"):
            raise SystemExit("Image advisory scan is incomplete")
        matches = data.get("matches")
        if not isinstance(matches, list):
            raise SystemExit("Image advisory matches missing")
        if (assessment is None) != (profile is None):
            reject("both assessment and runtime evidence are required")
        now = now or datetime.now(UTC)
        validate_scan(data, now)
        if assessment is not None:
            reviewed(
                data,
                read_json(assessment),
                read_json(profile),
                root=root,
                now=now,
            )
            print(
                json.dumps(
                    {
                        "raw_matching_advisories": len(matches),
                        "restricted_profile_dispositions_verified": len(matches),
                        "general_clean_scan_claim": False,
                    }
                )
            )
        elif matches:
            safe = sorted(
                {
                    (m["vulnerability"]["id"], m["artifact"]["name"], m["artifact"]["version"])
                    for m in matches
                }
            )
            print(
                json.dumps(
                    {"unresolved_image_advisories": len(safe), "sample": safe[:20]}, indent=2
                )
            )
            raise SystemExit(1)
        else:
            print("Image scan has no unresolved matching advisories")
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        reject("missing or invalid evidence")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--assessment", type=Path)
    parser.add_argument("--profile", type=Path)
    arguments = parser.parse_args()
    check(arguments.report, assessment=arguments.assessment, profile=arguments.profile)
