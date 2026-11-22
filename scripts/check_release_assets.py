"""Fetch and verify only tracked immutable private release assets; never unpack."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

REPOSITORY = "stefanrossmeier/llm-gateway"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
HEX = re.compile(r"[0-9a-f]{64}")


def unambiguous(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest key")
        result[key] = value
    return result


def manifest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError("invalid manifest file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("manifest grew past limit")
        data = json.loads(raw, object_pairs_hook=unambiguous)
    finally:
        os.close(fd)
    if (
        data["schema_version"] != 1
        or type(data["schema_version"]) is not int
        or data["repository"] != REPOSITORY
        or re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?", data["tag"]) is None
        or re.fullmatch(r"[0-9a-f]{40}", data["source_commit"]) is None
        or HEX.fullmatch(data["authored_source_sha256"]) is None
        or set(data["platforms"]) != {"arm64", "amd64"}
        or not isinstance(data["assets"], dict)
        or not 1 <= len(data["assets"]) <= 32
    ):
        raise ValueError("invalid release identity")
    total = 0
    for name, item in data["assets"].items():
        if (
            not NAME.fullmatch(name)
            or ".." in name
            or HEX.fullmatch(item["sha256"]) is None
            or type(item["size"]) is not int
            or not 0 < item["size"] <= 1024 * 1024 * 1024
        ):
            raise ValueError("invalid release asset")
        total += item["size"]
    if total > 4 * 1024 * 1024 * 1024:
        raise ValueError("release assets exceed total bound")
    for platform in data["platforms"].values():
        if set(platform) != {"image", "profile", "sbom", "original_scan"} or any(
            name not in data["assets"] for name in platform.values()
        ):
            raise ValueError("missing platform evidence")
    if data["platforms"]["arm64"]["image"] == data["platforms"]["amd64"]["image"]:
        raise ValueError("platform images must be distinct")
    return data


def verify(data, directory):
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise ValueError("absolute regular asset directory required")
    for name, expected in data["assets"].items():
        fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != expected["size"]:
                raise ValueError("release asset size/type mismatch")
            digest = hashlib.sha256()
            read = 0
            with os.fdopen(fd, "rb", closefd=False) as stream:
                while chunk := stream.read(1024 * 1024):
                    read += len(chunk)
                    if read > expected["size"]:
                        raise ValueError("release asset grew past bound")
                    digest.update(chunk)
            after = os.fstat(fd)
            if digest.hexdigest() != expected["sha256"] or (
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("release asset digest/identity mismatch")
        finally:
            os.close(fd)


def download(data, directory):
    if not directory.is_absolute():
        raise ValueError("absolute new download directory required")
    directory.mkdir(mode=0o700)  # Never overwrite an existing download/cache.
    for name in data["assets"]:
        subprocess.run(
            [
                "gh",
                "release",
                "download",
                data["tag"],
                "--repo",
                REPOSITORY,
                "--dir",
                str(directory),
                "--pattern",
                name,
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            timeout=300,
        )
    verify(data, directory)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args(argv)
    try:
        data = manifest(args.manifest)
        if args.download:
            download(data, args.directory)
        else:
            verify(data, args.directory)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise SystemExit("Release assets rejected; no image was unpacked or executed") from None
    print(json.dumps({"status": "verified", "tag": data["tag"], "assets": len(data["assets"])}))


if __name__ == "__main__":
    main()
