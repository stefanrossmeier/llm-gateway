import json
import re
import runpy
from pathlib import Path

import pytest

from llm_gateway.config import load_project_config
from llm_gateway.config.models import PrincipalConfig
from llm_gateway.contracts import ChatInput, InferenceRequest, Message
from llm_gateway.service.engine import Authority, Engine
from llm_gateway.storage import StateStore

pytestmark = pytest.mark.offline
ROOT = Path(__file__).resolve().parents[2]


def test_all_local_markdown_links_resolve():
    for path in [*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md")]:
        for target in re.findall(r"\]\(([^\s)]+)\)", path.read_text()):
            if target.startswith(("http:", "https:", "mailto:", "#", "codex:")):
                continue
            assert (path.parent / target.split("#")[0]).exists(), (path, target)


def test_generated_schemas_match_current_public_contracts():
    module = runpy.run_path(str(ROOT / "scripts" / "export_schemas.py"))
    for name, schema in module["generated"]().items():
        assert json.loads((ROOT / "docs" / "api" / name).read_text()) == schema, name


def test_readme_generator_produces_working_key_free_consumer_project(tmp_path):
    module = runpy.run_path(str(ROOT / "scripts" / "create_fake_project.py"))
    module["create"](tmp_path)
    snapshot = load_project_config(tmp_path / "project.yaml")
    engine = Engine(StateStore(tmp_path / "state"))
    engine.registry.activate(snapshot)
    authority = Authority(
        snapshot.project_id,
        "owner-ipc",
        PrincipalConfig(
            token_file="/unused",
            aliases=["example-chat"],
            operations=["chat"],
            permissions=["infer", "receipts:own"],
        ),
    )
    result = engine.run(
        authority,
        InferenceRequest(
            request_id="demo:one",
            alias="example-chat",
            operation="chat",
            input=ChatInput(messages=[Message(role="user", content="Hello")]),
        ),
    )
    assert result.content == "Hello from your project's fake fixture.\n"
    assert result.receipts[0].evidence_status == "simulated"
    assert engine.lookup(authority, "demo:one").output_status == "unavailable"
