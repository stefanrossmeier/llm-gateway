from datetime import UTC, datetime, timedelta

import pytest
from test_engine import configured
from test_ledger import request

from llm_gateway.config.models import EvidencePolicy

pytestmark = pytest.mark.offline


def test_prompt_retention_needs_ttl_and_is_independent_of_result_retention(tmp_path):
    with pytest.raises(ValueError, match="positive TTL"):
        EvidencePolicy(retain_prompts=True)
    engine, authority, _ = configured(
        tmp_path,
        mutate=lambda data: data.update(
            evidence_policy={"retain_prompts": True, "result_ttl_seconds": 10}
        ),
    )
    result = engine.run(authority, request())
    assert result.content == "ok"
    assert engine.lookup(authority, "one").output_status == "unavailable"
    with engine.store.connection() as db:
        prompt = db.execute("SELECT payload FROM retained_prompts").fetchone()[0]
        assert '"content":"public"' in prompt
        assert db.execute("SELECT COUNT(*) FROM retained_results").fetchone()[0] == 0
    engine.results.get(
        authority.project, authority.principal, "one", now=datetime.now(UTC) + timedelta(seconds=11)
    )
    with engine.store.connection() as db:
        assert db.execute("SELECT payload FROM retained_prompts").fetchone()[0] is None
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1


def test_default_keeps_no_prompt_rows(tmp_path):
    engine, authority, _ = configured(tmp_path)
    engine.run(authority, request())
    with engine.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM retained_prompts").fetchone()[0] == 0
