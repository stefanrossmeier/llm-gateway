from pathlib import Path

import pytest
from test_config import free_config, write_config

from llm_gateway.config import ConfigError, load_project_config
from llm_gateway.credentials import provision_gateway_token, read_private_secret
from llm_gateway.service.cli import main

pytestmark = pytest.mark.offline


def test_creation_rotation_and_cli_never_print_token(tmp_path, capsys):
    private = tmp_path / "secrets"
    private.mkdir(mode=0o700)
    path = private / "gateway.token"
    assert main(["token", "create", "--file", str(path)]) == 0
    old = read_private_secret(str(path))
    assert len(old) == 64 and path.stat().st_mode & 0o077 == 0
    assert old not in capsys.readouterr().out
    assert main(["token", "create", "--file", str(path)]) == 2
    assert read_private_secret(str(path)) == old
    assert main(["token", "create", "--file", str(path), "--rotate"]) == 0
    new = read_private_secret(str(path))
    captured = capsys.readouterr()
    assert (
        new != old
        and new not in captured.out + captured.err
        and old not in captured.out + captured.err
    )


def test_rotation_failure_before_replace_preserves_old_secret(tmp_path, monkeypatch):
    import llm_gateway.credentials as module

    path = tmp_path / "gateway.token"
    provision_gateway_token(path)
    old = path.read_bytes()

    def fail(*args):
        raise OSError("injected replace failure")

    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(ValueError, match="provision_failed"):
        provision_gateway_token(path, rotate=True)
    assert path.read_bytes() == old
    assert list(tmp_path.iterdir()) == [path]


def test_token_paths_and_permissions_fail_closed(tmp_path):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        provision_gateway_token(public / "gateway.token")
    with pytest.raises(ValueError):
        provision_gateway_token(tmp_path / "missing.token", rotate=True)
    with pytest.raises(ValueError):
        provision_gateway_token(Path("relative.token"))


def test_gateway_token_cannot_reuse_upstream_key(tmp_path):
    data = free_config()
    path = tmp_path / "gateway.token"
    provision_gateway_token(path)
    data["access"] = {"principals": {"owner": {"token_file": str(path)}}}
    key = read_private_secret(str(path))
    with pytest.raises(ConfigError, match="reuses_upstream") as caught:
        load_project_config(write_config(tmp_path, data), environment={"EXAMPLE_ROUTER_KEY": key})
    assert key not in str(caught.value)


@pytest.mark.parametrize("variable", ["HOME", "PYTHONPATH", "SSL_CERT_FILE", "LITELLM_MODE"])
def test_credential_reference_cannot_overwrite_owned_process_controls(variable):
    from pydantic import ValidationError

    from llm_gateway.config.models import CredentialReference

    with pytest.raises(ValidationError, match="runtime controls"):
        CredentialReference(env_var=variable)
