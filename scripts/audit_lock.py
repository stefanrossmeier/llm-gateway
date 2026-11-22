"""Static PyPI-wheel integrity and dated advisory evidence; never imports target packages.

Does not claim to detect all malware. An absent advisory is not proof of safety.
Run before installing a new lock, then review any hooks/findings manually.
"""

import argparse
import base64
import concurrent.futures
import csv
import hashlib
import io
import json
import sys
import tomllib
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

REVIEWED_DEV_HOOKS = {
    (
        "coverage",
        "7.17.2",
        "a1_coverage.pth",
    ): "ef2ed06d19867ec669c09a804060666a9cd5e383af0a9d11aa2de79b77d448e8",
}

DENY = {("litellm", version) for version in ("1.82.7", "1.82.8", "1.107.0")}


def fetch_json(url, data=None):
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "llm-gateway-static-review"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def inspect_wheel(artifact, cache):
    filename = artifact["url"].rsplit("/", 1)[-1]
    path = cache / filename
    expected = artifact["hash"].removeprefix("sha256:")
    if not path.exists():
        with urllib.request.urlopen(artifact["url"], timeout=90) as response:
            body = response.read()
        path.write_bytes(body)
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != expected:
        raise ValueError(f"wheel hash mismatch: {filename}")
    hooks = []
    native = []
    with zipfile.ZipFile(io.BytesIO(body)) as wheel:
        names = wheel.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate wheel members: {filename}")
        for name in names:
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or "\\" in name:
                raise ValueError(f"unsafe wheel member: {filename}")
            if name.endswith((".pth", "sitecustomize.py", "usercustomize.py")):
                hooks.append({"path": name, "sha256": hashlib.sha256(wheel.read(name)).hexdigest()})
            if name.endswith((".so", ".pyd", ".dylib", ".dll")):
                native.append(name)
        records = [name for name in names if name.endswith(".dist-info/RECORD")]
        if len(records) != 1:
            raise ValueError(f"wheel RECORD missing/ambiguous: {filename}")
        record_paths = set()
        for name, digest, size in csv.reader(io.StringIO(wheel.read(records[0]).decode())):
            record_paths.add(name)
            if digest:
                algorithm, encoded = digest.split("=", 1)
                if algorithm != "sha256":
                    raise ValueError(f"unexpected RECORD hash: {filename}")
                actual = base64.urlsafe_b64encode(hashlib.sha256(wheel.read(name)).digest())
                if actual.rstrip(b"=") != encoded.encode():
                    raise ValueError(f"RECORD mismatch: {filename}/{name}")
                if size and len(wheel.read(name)) != int(size):
                    raise ValueError(f"RECORD size mismatch: {filename}/{name}")
        if any(name not in record_paths for name in names if not name.endswith("/")):
            raise ValueError(f"unrecorded wheel member: {filename}")
    return dict(
        filename=filename,
        sha256=expected,
        record_verified=True,
        install_hooks=hooks,
        native_binaries=native,
    )


def review(package, cache):
    name, version = package["name"], package["version"]
    metadata = fetch_json(f"https://pypi.org/pypi/{name}/{version}/json")
    vulnerabilities = [v for v in metadata.get("vulnerabilities", []) if not v.get("withdrawn")]
    artifacts = package.get("wheels", [])
    # Review all pure wheels and the actual macOS arm64 and Linux x86_64/arm64 deployment wheels.
    selected = [
        w
        for w in artifacts
        if any(
            tag in w["url"]
            for tag in (
                "none-any.whl",
                "macosx_11_0_arm64",
                "macosx_12_0_arm64",
                "macosx_10_9_universal2",
                "manylinux_2_17_x86_64",
                "manylinux_2_28_x86_64",
                "manylinux2014_x86_64",
                "manylinux_2_17_aarch64",
                "manylinux_2_28_aarch64",
                "manylinux2014_aarch64",
            )
        )
        and (
            "cp311-" in w["url"]
            or "cp312-" in w["url"]
            or "abi3" in w["url"]
            or "none-any.whl" in w["url"]
        )
    ]
    if not selected:
        # Record a wheel even if the project's runtime does not use this platform-only dependency.
        selected = artifacts[:1]
    wheels = [inspect_wheel(w, cache) for w in selected]
    for wheel in wheels:
        for hook in wheel["install_hooks"]:
            hook["reviewed_dev_only"] = (
                REVIEWED_DEV_HOOKS.get((name, version, hook["path"])) == hook["sha256"]
            )
    return dict(
        name=name,
        version=version,
        pypi_vulnerabilities=vulnerabilities,
        known_compromised=(name, version) in DENY,
        wheels=wheels,
        limitations="Static integrity/hook review; native binary behavior is not certified.",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    raw = args.lock.read_bytes()
    packages = [p for p in tomllib.loads(raw.decode())["package"] if "registry" in p["source"]]
    query = {
        "queries": [
            {"package": {"name": p["name"], "ecosystem": "PyPI"}, "version": p["version"]}
            for p in packages
        ]
    }
    osv = fetch_json("https://api.osv.dev/v1/querybatch", json.dumps(query).encode())
    if len(osv.get("results", [])) != len(packages):
        raise ValueError("Incomplete OSV response; audit cannot pass")
    print(f"OSV queried {len(packages)} locked packages", flush=True)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(review, p, args.cache): (p, osv["results"][i])
            for i, p in enumerate(packages)
        }
        for future in concurrent.futures.as_completed(futures):
            package, result = futures[future]
            reviewed = future.result()
            reviewed["osv"] = result.get("vulns", [])
            results.append(reviewed)
            print(f"Reviewed {package['name']}=={package['version']}", flush=True)
    findings = [
        p
        for p in results
        if p["known_compromised"]
        or p["osv"]
        or p["pypi_vulnerabilities"]
        or any(not h["reviewed_dev_only"] for w in p["wheels"] for h in w["install_hooks"])
    ]
    report = dict(
        reviewed_at=datetime.now(UTC).isoformat(),
        lock_sha256=hashlib.sha256(raw).hexdigest(),
        sources=["https://api.osv.dev/v1/querybatch", "https://pypi.org"],
        packages=sorted(results, key=lambda p: p["name"]),
        automated_findings=findings,
        target_code_executed=False,
        scope="Locked registry graph; selected pure/macOS arm64/Linux x86_64+arm64 wheels.",
        status="manual_review_required" if findings else "no_automated_findings",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {"packages": len(results), "findings": len(findings), "output": str(args.output)}
        )
    )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
