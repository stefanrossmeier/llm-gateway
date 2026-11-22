"""Bounded project config and captured-file trust boundaries."""

import hashlib
import json
import os
from dataclasses import replace

import pytest
from test_config import private_file, synthetic_config, write_config
from test_paid_proposal import project

from llm_gateway.config import ConfigError, load_project_config
from llm_gateway.config.loader import canonical_json, parse_yaml, read_bounded_file

pytestmark = pytest.mark.offline


def test_non_scalar_yaml_keys_and_nonregular_or_oversized_config_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match="key_invalid"):
        parse_yaml(b"? [one, two]\n: value\n")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ConfigError, match="file_invalid"):
        read_bounded_file(fifo)
    file = tmp_path / "oversize"
    file.write_bytes(b"x" * 32)
    with pytest.raises(ConfigError, match="file_invalid"):
        read_bounded_file(file, limit=31)


@pytest.mark.parametrize(
    "kind", ["root_missing", "missing_file", "escape", "directory", "oversize", "bad_encoding"]
)
def test_fixture_capture_rejects_unavailable_escaped_nontext_or_unbounded_files(tmp_path, kind):
    root = tmp_path / "fixtures"
    root.mkdir()
    leaf = root / "reply.txt"
    leaf.write_text("public")
    data = synthetic_config()
    data["fake"] = {"fixture_root": str(root), "responses": {"summary": {"default": ["reply.txt"]}}}
    if kind == "root_missing":
        data["fake"]["fixture_root"] = str(tmp_path / "missing")
    elif kind == "missing_file":
        leaf.unlink()
    elif kind == "escape":
        leaf.unlink()
        leaf.symlink_to(private_file(tmp_path, "outside.txt", "private-canary"))
    elif kind == "directory":
        leaf.unlink()
        leaf.mkdir()
    elif kind == "oversize":
        leaf.write_bytes(b"x" * 1048577)
    else:
        leaf.write_bytes(b"\xff")
    with pytest.raises(ConfigError):
        load_project_config(write_config(tmp_path, data))


def test_total_fixture_bound_counts_all_captured_entries(tmp_path):
    root = tmp_path / "fixtures"
    root.mkdir()
    (root / "reply.txt").write_bytes(b"x" * 1048576)
    data = synthetic_config()
    data["fake"] = {
        "fixture_root": str(root),
        "responses": {"summary": {"default": ["reply.txt"] * 17}},
    }
    with pytest.raises(ConfigError, match="fixture_total_limit"):
        load_project_config(write_config(tmp_path, data))


def test_unrecognized_capability_snapshot_is_integral_but_not_executable(tmp_path):
    snapshot = load_project_config(write_config(tmp_path, synthetic_config()))
    revision = "unreviewed-future-adapter"
    digest = hashlib.sha256(
        canonical_json(
            {
                "project_config": json.loads(snapshot.payload),
                "fixtures": snapshot.fixtures,
                "capability_revision": revision,
            }
        ).encode()
    ).hexdigest()
    unknown = replace(snapshot, digest=digest, capability_revision=revision)
    with pytest.raises(ConfigError, match="snapshot_capability_unavailable"):
        _ = unknown.config


def test_gateway_bearer_must_not_reuse_upstream_provider_key(tmp_path):
    snapshot = project(tmp_path)
    data = snapshot.config.model_dump(mode="json")
    token = private_file(tmp_path, "token", "synthetic-credential-over-thirty-two-characters")
    data["access"]["principals"]["test"]["token_file"] = str(token)
    environment = {}
    for provider in data["providers"].values():
        environment[provider["credential"]["env_var"]] = token.read_text()
    with pytest.raises(ConfigError, match="gateway_token_reuses_upstream_credential"):
        load_project_config(write_config(tmp_path, data), environment=environment)
