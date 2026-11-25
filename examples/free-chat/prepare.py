"""Verify and load the platform's released Docker archive; no gateway source build."""

import hashlib
import json
import re
import subprocess
import urllib.request
from pathlib import Path

VERSION = "0.1.0a4"
root = Path(__file__).resolve().parent
manifest = json.loads((root.parents[1] / "release" / f"{VERSION}.json").read_text())
docker_arch = subprocess.check_output(
    ["docker", "info", "--format", "{{.Architecture}}"], text=True
).strip()
architecture = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(
    docker_arch
)
if architecture is None:
    raise SystemExit("This release supports ARM64 and AMD64 Docker hosts")
name = manifest["platforms"][architecture]["image"]
if not re.fullmatch(r"image-(?:arm64|amd64)\.tar", name) or manifest["tag"] != f"v{VERSION}":
    raise SystemExit("Release manifest identity rejected")
expected = manifest["assets"][name]
directory = root / ".release-assets"
directory.mkdir(exist_ok=True)
archive = directory / name
if not archive.exists():
    url = f"https://github.com/stefanrossmeier/llm-gateway/releases/download/v{VERSION}/{name}"
    print(f"Downloading {name} from v{VERSION}…", flush=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("xb") as output:
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > expected["size"]:
                    raise ValueError("Archive exceeds manifest size")
                output.write(chunk)
    except Exception:
        archive.unlink(missing_ok=True)
        raise
digest = hashlib.sha256()
with archive.open("rb") as stream:
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
if archive.stat().st_size != expected["size"] or digest.hexdigest() != expected["sha256"]:
    raise SystemExit(f"Archive checksum rejected; remove {archive} before downloading again")
loaded = subprocess.run(
    ["docker", "load", "--input", str(archive)], check=True, capture_output=True, text=True
)
references = re.findall(r"^Loaded image(?: ID)?: (\S+)$", loaded.stdout, re.MULTILINE)
if len(references) != 1:
    raise SystemExit("Expected exactly one released image")
identity = subprocess.check_output(
    ["docker", "image", "inspect", "--format", "{{.Id}}", references[0]], text=True
).strip()
if not re.fullmatch(r"sha256:[a-f0-9]{64}", identity):
    raise SystemExit("Invalid loaded image identity")
(root / ".release.env").write_text(f"LLM_GATEWAY_IMAGE={identity}\n")
print(
    f"Verified v{VERSION} ({architecture}). Run docker compose --env-file .release.env up -d --wait"
)
