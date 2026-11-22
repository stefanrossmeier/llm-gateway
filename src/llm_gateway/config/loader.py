"""Explicit local YAML, bounded captured fixtures and immutable nonsecret snapshots."""

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml
from pydantic import ValidationError
from yaml.tokens import AliasToken, AnchorToken

from .models import CAPABILITY_REVISION, ProjectConfig
from .secrets import SecretUnavailable, read_private_secret, resolve_credential


class ConfigError(ValueError):
    def __init__(self, code: str, locations: list[str] | None = None):
        self.code = code
        self.locations = locations or []
        super().__init__(code)


class UniqueSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = set()
        for key, _ in node.value:
            if not isinstance(key, yaml.ScalarNode):
                raise ConfigError("configuration_key_invalid")
            if key.value in keys:
                raise ConfigError("configuration_duplicate_key")
            if key.value == "<<":
                raise ConfigError("configuration_yaml_merge_forbidden")
            keys.add(key.value)
        return super().construct_mapping(node, deep=deep)


def read_bounded_file(path: Path, limit: int = 1048576) -> bytes:
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ConfigError("configuration_file_invalid")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ConfigError("configuration_file_too_large")
        return raw
    except OSError as error:
        raise ConfigError("configuration_file_unavailable") from error
    finally:
        if fd is not None:
            os.close(fd)


def parse_yaml(raw: bytes) -> dict:
    try:
        depth = 0
        count = 0
        for token in yaml.scan(raw):
            count += 1
            if isinstance(token, (AliasToken, AnchorToken)):
                raise ConfigError("configuration_yaml_alias_forbidden")
            if token.__class__.__name__ in {
                "BlockMappingStartToken",
                "BlockSequenceStartToken",
                "FlowMappingStartToken",
                "FlowSequenceStartToken",
            }:
                depth += 1
            if token.__class__.__name__ in {
                "BlockEndToken",
                "FlowMappingEndToken",
                "FlowSequenceEndToken",
            }:
                depth -= 1
            if depth > 64 or count > 32768:
                raise ConfigError("configuration_yaml_limit")
        value = yaml.load(raw, Loader=UniqueSafeLoader)
        if not isinstance(value, dict):
            raise ConfigError("configuration_mapping_required")
        return value
    except (yaml.YAMLError, UnicodeError, RecursionError, TypeError) as error:
        raise ConfigError("configuration_yaml_invalid") from error


def canonical_json(data: object) -> str:
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


@dataclass(frozen=True)
class ConfigSnapshot:
    project_id: str
    digest: str
    payload: str
    fixtures_payload: str
    capability_revision: str = CAPABILITY_REVISION

    def __post_init__(self):
        try:
            payload = json.loads(self.payload)
            expected = hashlib.sha256(
                canonical_json(
                    {
                        "project_config": payload,
                        "fixtures": json.loads(self.fixtures_payload),
                        "capability_revision": self.capability_revision,
                    }
                ).encode()
            ).hexdigest()
            if payload.get("project_id") != self.project_id or expected != self.digest:
                raise ConfigError("snapshot_integrity_conflict")
        except (ValueError, TypeError, AttributeError):
            raise ConfigError("snapshot_integrity_conflict") from None

    @property
    def config(self) -> ProjectConfig:
        # Never expose the authoritative nested containers for mutation.
        if self.capability_revision not in {
            CAPABILITY_REVISION,
            "litellm-1.109.0/gateway-v1",
            "litellm-1.109.0/gateway-v2",
            "litellm-1.109.0/gateway-v3",
        }:
            raise ConfigError("snapshot_capability_unavailable")
        return ProjectConfig.model_validate_json(
            self.payload,
            context={"legacy_snapshot": self.capability_revision != CAPABILITY_REVISION},
        )

    @property
    def fixtures(self) -> dict:
        return json.loads(self.fixtures_payload)


def capture_fixtures(config: ProjectConfig) -> dict:
    if config.fake is None:
        return {}
    root = Path(config.fake.fixture_root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ConfigError("fixture_root_invalid")
    root = root.resolve()
    captures = {}
    total_bytes = 0
    for stages in config.fake.responses.values():
        for paths in stages.values():
            for raw_path in paths:
                path = PurePosixPath(raw_path)
                if (
                    not path.parts
                    or path.is_absolute()
                    or ".." in path.parts
                    or ":" in raw_path
                    or "\\" in raw_path
                ):
                    raise ConfigError("fixture_path_invalid")
                try:
                    resolved = (root / raw_path).resolve(strict=True)
                except OSError as error:
                    raise ConfigError("fixture_file_unavailable") from error
                if root not in resolved.parents:
                    raise ConfigError("fixture_path_escape")
                # Walk every segment relative to the opened mount root; a parent
                # symlink swap cannot redirect the final open outside that root.
                descriptors = []
                try:
                    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    descriptors.append(directory)
                    for part in path.parts[:-1]:
                        directory = os.open(
                            part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                        )
                        descriptors.append(directory)
                    leaf = os.open(
                        path.parts[-1],
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory,
                    )
                    descriptors.append(leaf)
                    info = os.fstat(leaf)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 1048576:
                        raise ConfigError("fixture_file_invalid")
                    with os.fdopen(leaf, "rb", closefd=False) as stream:
                        data = stream.read(1048577)
                    if len(data) > 1048576:
                        raise ConfigError("fixture_file_too_large")
                except OSError:
                    raise ConfigError("fixture_path_invalid") from None
                finally:
                    for descriptor in reversed(descriptors):
                        os.close(descriptor)
                total_bytes += len(data)
                if total_bytes > 16777216:
                    raise ConfigError("fixture_total_limit")
                try:
                    captures[raw_path] = {
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "content": data.decode("utf-8"),
                    }
                except UnicodeError as error:
                    raise ConfigError("fixture_encoding_invalid") from error
    return captures


def load_project_config(
    path: Path,
    *,
    expected_project_id: str | None = None,
    environment: Mapping[str, str] | None = None,
    check_credentials: bool = True,
) -> ConfigSnapshot:
    env = os.environ if environment is None else environment
    raw = parse_yaml(read_bounded_file(path))
    overrides = raw.get("overrides", {})
    if isinstance(overrides, dict):
        for alias, variable in overrides.items():
            if isinstance(variable, str) and isinstance(raw.get("aliases"), dict):
                if any(word in variable for word in ("KEY", "TOKEN", "PASSWORD", "SECRET")):
                    raise ConfigError("model_override_secret_reference_forbidden")
                value = env.get(variable, "").strip()
                profile = raw["aliases"].get(alias)
                if value and isinstance(profile, dict):
                    profile["upstream_model"] = value
    try:
        config = ProjectConfig.model_validate(raw)
    except ValidationError as error:
        # Never print raw validation inputs or YAML excerpts containing secret values.
        fields = {
            "schema_version",
            "project_id",
            "mode",
            "timezone",
            "providers",
            "aliases",
            "budgets",
            "access",
            "operation_policies",
            "compatibility",
            "overrides",
            "fake",
            "evidence_policy",
            "limits",
            "pricing",
            "capabilities",
            "provider_ref",
            "max_output_tokens",
            "max_input_bytes",
            "max_input_tokens",
            "token_file",
            "credential",
            "generation",
            "attempt_policy",
            "upstream_model",
        }
        locations = [
            ".".join(str(part) if part in fields else "*" for part in item["loc"])
            for item in error.errors(include_input=False, include_url=False)
        ]
        raise ConfigError("configuration_schema_invalid", locations) from None
    if expected_project_id and config.project_id != expected_project_id:
        raise ConfigError("configuration_project_mismatch")
    if config.mode == "live" and check_credentials:
        try:
            upstream_values = set()
            used = {alias.provider_ref for alias in config.aliases.values()}
            used.update(
                route.provider_ref
                for alias in config.aliases.values()
                for route in alias.attempt_policy.fallback_routes
            )
            for name, provider in config.providers.items():
                if name in used and provider.enabled and provider.credential is not None:
                    upstream_values.add(resolve_credential(provider.credential, env))
            tokens = set()
            for principal in config.access.principals.values():
                token = read_private_secret(principal.token_file)
                if len(token.encode()) < 32 or token in tokens:
                    raise ConfigError("gateway_token_invalid")
                if token in upstream_values:
                    raise ConfigError("gateway_token_reuses_upstream_credential")
                tokens.add(token)
        except SecretUnavailable as error:
            raise ConfigError("configuration_credential_unavailable") from error
    captures = capture_fixtures(config)
    payload = canonical_json(config.model_dump(mode="json"))
    fixtures_payload = canonical_json(captures)
    digest = hashlib.sha256(
        canonical_json(
            {
                "project_config": json.loads(payload),
                "fixtures": captures,
                "capability_revision": CAPABILITY_REVISION,
            }
        ).encode()
    ).hexdigest()
    return ConfigSnapshot(config.project_id, digest, payload, fixtures_payload)
