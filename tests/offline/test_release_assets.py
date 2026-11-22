"""Immutable release admission, not a synthetic claim about real artifact safety."""

import hashlib
import json
import os
import runpy
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.offline
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_release_assets.py"


def fixture(tmp_path):
    directory = tmp_path / "assets"
    directory.mkdir()
    data = {
        "schema_version": 1,
        "repository": "stefanrossmeier/llm-gateway",
        "tag": "v0.1.0a1",
        "source_commit": "a" * 40,
        "authored_source_sha256": "b" * 64,
        "assets": {},
        "platforms": {},
    }
    for arch in ("arm64", "amd64"):
        platform = {}
        for kind, extension in [
            ("image", "tar"),
            ("profile", "json"),
            ("sbom", "json"),
            ("original_scan", "json"),
        ]:
            name = f"{kind}-{arch}.{extension}"
            body = f"synthetic {name}".encode()
            (directory / name).write_bytes(body)
            data["assets"][name] = {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
            platform[kind] = name
        data["platforms"][arch] = platform
    path = tmp_path / "release.json"
    path.write_text(json.dumps(data))
    return runpy.run_path(str(SCRIPT)), path, directory, data


def test_manifest_and_exact_assets_pass_without_unpacking_or_execution(tmp_path):
    script, path, directory, data = fixture(tmp_path)
    assert script["manifest"](path) == data
    assert script["verify"](data, directory) is None
    script["main"]([str(path), str(directory)])


@pytest.mark.parametrize(
    "kind",
    [
        "repo",
        "tag",
        "source",
        "digest",
        "schema",
        "platform",
        "missing",
        "same_image",
        "traversal",
        "size",
        "bool",
        "hash",
    ],
)
def test_manifest_rejects_wrong_identity_and_unsafe_or_missing_assets(tmp_path, kind):
    script, path, _, data = fixture(tmp_path)
    if kind in {"repo", "tag", "source", "digest", "schema"}:
        key = {
            "repo": "repository",
            "tag": "tag",
            "source": "source_commit",
            "digest": "authored_source_sha256",
            "schema": "schema_version",
        }[kind]
        data[key] = {
            "repo": "attacker/other",
            "tag": "--clobber",
            "source": "main",
            "digest": "0",
            "schema": True,
        }[kind]
    elif kind == "platform":
        data["platforms"].pop("amd64")
    elif kind == "missing":
        data["assets"].pop("image-arm64.tar")
    elif kind == "same_image":
        data["platforms"]["amd64"]["image"] = "image-arm64.tar"
    elif kind == "traversal":
        data["assets"]["../escape"] = data["assets"].pop("image-arm64.tar")
    else:
        data["assets"]["image-arm64.tar"].update(
            {"size": {"size": 1024**3 + 1, "bool": True}.get(kind, 1)}
            if kind != "hash"
            else {"sha256": "malformed"}
        )
    path.write_text(json.dumps(data))
    with pytest.raises(SystemExit, match="Release assets rejected"):
        script["main"]([str(path), str(path.parent / "unused")])


@pytest.mark.parametrize("kind", ["duplicate", "fifo", "symlink", "oversize"])
def test_manifest_file_boundaries_fail_safely(tmp_path, kind):
    script, path, directory, _ = fixture(tmp_path)
    if kind == "duplicate":
        path.write_text('{"schema_version":1,"schema_version":1}')
    elif kind == "oversize":
        path.write_bytes(b" " * (1024**2 + 1))
    else:
        path.unlink()
        if kind == "fifo":
            os.mkfifo(path)
        else:
            path.symlink_to(directory / "profile-arm64.json")
    with pytest.raises(SystemExit, match="Release assets rejected"):
        script["main"]([str(path), str(directory)])


@pytest.mark.parametrize("kind", ["changed", "truncated", "fifo", "symlink", "missing"])
def test_modified_or_nonregular_asset_is_never_accepted(tmp_path, kind):
    script, path, directory, _ = fixture(tmp_path)
    item = directory / "image-arm64.tar"
    if kind == "changed":
        item.write_bytes(b"x" * item.stat().st_size)
    elif kind == "truncated":
        item.write_bytes(b"x")
    else:
        item.unlink()
        if kind == "fifo":
            os.mkfifo(item)
        elif kind == "symlink":
            item.symlink_to(directory / "image-amd64.tar")
    with pytest.raises(SystemExit, match="Release assets rejected"):
        script["main"]([str(path), str(directory)])


def test_download_uses_fixed_repository_exact_names_and_refuses_overwrite(tmp_path, monkeypatch):
    script, _, old, data = fixture(tmp_path)
    calls = []

    def download(args, **kwargs):
        assert args[:3] == ["gh", "release", "download"]
        assert args[args.index("--repo") + 1] == "stefanrossmeier/llm-gateway"
        assert kwargs == {"check": True, "stdin": subprocess.DEVNULL, "timeout": 300}
        directory = Path(args[args.index("--dir") + 1])
        name = args[args.index("--pattern") + 1]
        calls.append(name)
        (directory / name).write_bytes((old / name).read_bytes())

    monkeypatch.setattr(subprocess, "run", download)
    directory = tmp_path / "new-download"
    script["download"](data, directory)
    assert calls == list(data["assets"])
    assert directory.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        script["download"](data, directory)
    assert len(calls) == len(data["assets"])


def test_download_failure_is_safe_and_cannot_claim_verified(tmp_path, monkeypatch):
    script, path, _, _ = fixture(tmp_path)

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("gh", 300)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(SystemExit, match="no image was unpacked or executed"):
        script["main"]([str(path), str(tmp_path / "failed"), "--download"])
