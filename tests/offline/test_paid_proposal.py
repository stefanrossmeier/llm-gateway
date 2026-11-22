"""No paid inference: project-owned proposals and blocked-bound behavior only."""

import copy
from datetime import UTC, date, datetime, timedelta

import pytest
from test_config import write_config
from test_live_preflight import setup

from llm_gateway.config import load_project_config
from llm_gateway.providers.pricing import reserve_usd
from llm_gateway.testing.preflight import PaidApproval, PreflightError, preflight
from llm_gateway.testing.proposal import propose_paid_plan

pytestmark = pytest.mark.offline


def project(tmp_path):
    _, snapshot = setup(tmp_path, paid=True)
    config = snapshot.config.model_dump(mode="json")
    base = config["aliases"]["summary"]
    base["capabilities"] = ["plain_text", "json_schema"]
    base["limits"].update(max_input_bytes=8192, max_input_tokens=16384, max_output_tokens=128)
    config["providers"]["direct"] = {
        "kind": "openai",
        "credential": {"env_var": "SYNTHETIC_DIRECT_API_KEY"},
    }
    aliases = config["aliases"]
    aliases["response"] = copy.deepcopy(base) | {
        "provider_ref": "direct",
        "upstream_model": "synthetic-responses",
        "operation": "responses",
        "capabilities": ["plain_text", "function_calls"],
    }
    aliases["decision"] = copy.deepcopy(base) | {"operation": "decisions", "capabilities": []}
    aliases["decision"]["limits"]["max_output_tokens"] = None
    aliases["decision"]["pricing"].update(output_usd_per_token="0", reasoning_usd_per_token="0")
    aliases["embedding"] = copy.deepcopy(base) | {
        "provider_ref": "direct",
        "upstream_model": "synthetic-embedding",
        "operation": "embeddings",
        "capabilities": ["embedding_dimensions"],
    }
    aliases["embedding"]["limits"].update(max_output_tokens=None, embedding_dimensions=2)
    aliases["search"] = copy.deepcopy(base) | {
        "operation": "x_search",
        "capabilities": ["native_x_search", "json_schema"],
        "operation_policy_ref": "scope",
    }
    aliases["search"]["limits"]["max_tool_calls"] = 1
    aliases["search"]["pricing"]["tool_fees_usd"] = {"native_x_search": "0.001"}
    config["operation_policies"] = {
        "scope": {
            "kind": "x_search",
            "allowed_x_handles": ["public_example"],
            "max_handles": 1,
            "max_utc_days": 2,
            "max_posts": 6,
            "max_post_characters": 4000,
            "provider_order": ["xAI"],
            "allowed_domains": ["x.com"],
        }
    }
    config["access"]["principals"]["test"].update(
        aliases=list(aliases),
        operations=["chat", "responses", "decisions", "embeddings", "x_search"],
    )
    return load_project_config(write_config(tmp_path, config), check_credentials=False)


SELECTIONS = {
    "direct_responses": "response",
    "router_schema": "summary",
    "function_responses": "response",
    "decisions": "decision",
    "embeddings": "embedding",
    "native_x": "search",
}


def proposal(snapshot, selections=None, total="1"):
    return propose_paid_plan(
        snapshot,
        principal_id="test",
        run_id="run-one",
        selections=selections or SELECTIONS,
        total_ceiling_usd=total,
        today=date(2026, 11, 11),
    )


def test_all_paid_cases_are_key_free_bounded_proposals_not_authorization(tmp_path):
    snapshot = project(tmp_path)
    result = proposal(snapshot)
    assert result["execution_authorized"] is False and result["blocked_cases"] == [
        {"case_id": "native_x", "reason": "native_x_risk_acceptance_required"}
    ]
    plan = result["plan"]
    assert plan.max_calls == 5 and plan.group == "paid"
    for case in plan.cases:
        alias = snapshot.config.aliases[case.request.alias]
        assert case.request.limits.max_attempts == 1
        assert case.request.limits.max_usd == reserve_usd(
            alias, alias.limits.max_input_tokens, case.request.limits.max_output_tokens
        )
    assert plan.cases[0].request.request_id == "run-one:direct_responses"
    # Even a complete proposal cannot pass paid preflight without explicit approval.
    with pytest.raises(PreflightError, match="owner_approval"):
        preflight(plan, snapshot, enabled=True)
    now = datetime.now(UTC)
    fixture = PaidApproval(
        plan_digest=plan.digest,
        config_digest=snapshot.digest,
        total_ceiling_usd="1",
        approved_by="synthetic offline fixture; no human approval",
        approved_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(hours=1),
        implementation_and_documentation_complete=True,
    )
    assert preflight(plan, snapshot, enabled=True, approval=fixture)
    # The executor hard guard remains closed even for a syntactically valid record.
    from llm_gateway.testing.runner import run_plan

    class NoGateway:
        def __getattr__(self, _name):
            pytest.fail("Paid proposal accessed a gateway")

    report = run_plan(plan, snapshot, NoGateway(), enabled=True, approval=fixture)
    assert report["inference_posts_attempted"] == 0
    assert report["precondition"] == "paid_execution_waits_for_final_owner_approval"


def test_unenforceable_decisions_bound_is_blocked_without_expanding_budget(tmp_path):
    snapshot = project(tmp_path)
    data = snapshot.config.model_dump(mode="json")
    data["aliases"]["decision"]["pricing"]["output_usd_per_token"] = "0.00001"
    changed = load_project_config(write_config(tmp_path, data), check_credentials=False)
    result = proposal(changed, {"decisions": "decision"})
    assert result["plan"] is None and result["sum_case_ceilings_usd"] == "0"
    assert result["blocked_cases"] == [
        {"case_id": "decisions", "reason": "decision_output_bound_unavailable"}
    ]


@pytest.mark.parametrize(
    "selection",
    [
        {"direct_responses": "summary"},
        {"router_schema": "response"},
        {"embeddings": "does-not-exist"},
    ],
)
def test_wrong_operation_provider_or_missing_alias_cannot_create_a_paid_case(tmp_path, selection):
    result = proposal(project(tmp_path), selection)
    assert result["plan"] is None and len(result["blocked_cases"]) == 1


def test_total_below_persistent_authority_blocks_all_cases(tmp_path):
    result = proposal(project(tmp_path), total="0.01")
    assert result["plan"] is None
    assert all(
        c["reason"] == "paid_proposal_server_cap_exceeds_total" for c in result["blocked_cases"]
    )


def test_no_unknown_case_role_or_zero_total_is_accepted(tmp_path):
    snapshot = project(tmp_path)
    with pytest.raises(PreflightError):
        proposal(snapshot, {"silently-top-up": "summary"})
    with pytest.raises(PreflightError):
        proposal(snapshot, total="0")


def test_combined_scope_must_fit_even_when_individual_cases_fit(tmp_path):
    snapshot = project(tmp_path)
    data = snapshot.config.model_dump(mode="json")
    for policy in data["budgets"].values():
        policy["cap_usd"] = "0.07"
    changed = load_project_config(write_config(tmp_path, data), check_credentials=False)
    with pytest.raises(PreflightError, match="combined_scope"):
        proposal(changed)


def test_native_fee_or_workflow_authority_cannot_be_guessed(tmp_path):
    snapshot = project(tmp_path)
    data = snapshot.config.model_dump(mode="json")
    data["aliases"]["search"]["pricing"]["tool_fees_usd"] = {}
    changed = load_project_config(write_config(tmp_path, data), check_credentials=False)
    result = proposal(changed, {"native_x": "search"})
    assert result["plan"] is None
    assert result["blocked_cases"][0]["reason"] == "native_x_risk_acceptance_required"
    data = snapshot.config.model_dump(mode="json")
    data["budgets"]["run"]["scope"] = "workflow"
    changed = load_project_config(write_config(tmp_path, data), check_credentials=False)
    result = proposal(changed, {"router_schema": "summary"})
    assert result["plan"] is None
    assert result["blocked_cases"][0]["reason"] == "budget_scope_unbound"
