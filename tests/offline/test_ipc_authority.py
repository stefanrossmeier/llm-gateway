import pytest
from test_config import synthetic_config, write_config
from test_engine import configured
from test_ledger import request

from llm_gateway.config import ConfigError, load_project_config
from llm_gateway.service.engine import Authority, EngineError

pytestmark = pytest.mark.offline


def test_principal_name_cannot_claim_verified_ipc_paid_run_exception(tmp_path):
    engine, original, _ = configured(tmp_path)
    grants = original.grants.model_copy(update={"run_id": None})
    spoofed = Authority(original.project, "owner-ipc", grants)
    with pytest.raises(EngineError, match="paid_run_binding_required"):
        engine.run(spoofed, request())
    assert engine.provider.calls == []
    verified = Authority(original.project, "owner-ipc", grants, owner_ipc=True)
    result = engine.run(verified, request())
    assert result.execution_status == "completed"
    assert len(engine.provider.calls) == 1


def test_configured_bearer_cannot_collide_with_private_ipc_receipt_namespace(tmp_path):
    data = synthetic_config()
    data["access"] = {
        "principals": {
            "owner-ipc": {
                "token_file": "/unused",
                "permissions": ["infer"],
                "aliases": ["summary"],
                "operations": ["chat"],
            }
        }
    }
    with pytest.raises(ConfigError, match="schema_invalid"):
        load_project_config(write_config(tmp_path, data))


def test_historical_reserved_bearer_is_denied_without_reading_its_token(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from llm_gateway.service import auth

    engine, original, snapshot = configured(tmp_path)
    legacy = snapshot.config.model_copy(deep=True)
    legacy.access.principals.clear()
    legacy.access.principals["owner-ipc"] = original.grants
    monkeypatch.setattr(
        engine.registry,
        "get",
        lambda project: SimpleNamespace(config=legacy, digest=snapshot.digest),
    )

    def forbidden(*args):
        raise AssertionError("reserved bearer token should not be read")

    monkeypatch.setattr(auth, "read_private_secret", forbidden)
    with pytest.raises(EngineError, match="authentication_failed"):
        auth.Authenticator(engine.registry).authenticate(
            "Bearer " + "x" * 48, client=("127.0.0.1", 1)
        )
