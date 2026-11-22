"""Concurrency is explicit approval data; these tests never execute inference."""

import pytest
from pydantic import ValidationError
from test_config import write_config
from test_live_preflight import setup
from test_paid_executor import approval_for
from test_paid_proposal import project

from llm_gateway.config import load_project_config
from llm_gateway.contracts import ChatInput
from llm_gateway.testing.preflight import LivePlan, PreflightError, preflight
from llm_gateway.testing.proposal import propose_paid_smoke_pair
from llm_gateway.testing.runner import run_plan

pytestmark = pytest.mark.offline


def pair(snapshot, **changes):
    return propose_paid_smoke_pair(
        snapshot,
        principal_id="test",
        run_id="run-one",
        direct_alias="response",
        router_alias="summary",
        total_ceiling_usd="1",
        **changes,
    )


def test_original_sequential_v1_fixture_keeps_its_exact_digest(tmp_path):
    plan, _ = setup(tmp_path)
    assert plan.max_concurrency == 1
    assert plan.digest == "138ac9042d83440e9e42a3f0e00930c6e5ead860ec7b9dfedea68e646dea45ef"
    data = plan.model_dump(mode="json")
    data.pop("max_concurrency")
    assert LivePlan.model_validate(data).digest == plan.digest


def test_pure_pair_is_bounded_and_concurrency_invalidates_sequential_approval(tmp_path):
    snapshot = project(tmp_path)
    proposed = pair(snapshot)
    plan = proposed["plan"]
    assert plan.max_concurrency == 2 and len(plan.cases) == 2
    assert proposed["execution_authorized"] is False and proposed["blocked_cases"] == []
    assert (
        preflight(plan, snapshot, enabled=True, approval=approval_for(plan, snapshot))[
            "max_concurrency"
        ]
        == 2
    )
    sequential = plan.model_copy(update={"max_concurrency": 1})
    assert sequential.digest != plan.digest
    with pytest.raises(PreflightError, match="identity_or_expiry"):
        preflight(plan, snapshot, enabled=True, approval=approval_for(sequential, snapshot))


@pytest.mark.parametrize(
    "kind", ["free", "one_case", "three_cases", "too_many", "boolean", "string", "zero"]
)
def test_parallel_declaration_is_only_a_two_case_paid_plan(tmp_path, kind):
    data = pair(project(tmp_path))["plan"].model_dump(mode="json")
    if kind == "free":
        data.update(group="free", total_ceiling_usd="0")
    elif kind == "one_case":
        data["cases"].pop()
    elif kind == "three_cases":
        third = data["cases"][0].copy() | {"case_id": "third"}
        third["request"] = third["request"].copy() | {"request_id": "third"}
        data["cases"].append(third)
        data["max_calls"] = 3
    else:
        data["max_concurrency"] = {"too_many": 3, "boolean": True, "string": "2", "zero": 0}[kind]
    with pytest.raises(ValidationError):
        LivePlan.model_validate(data)


@pytest.mark.parametrize("kind", ["one_provider", "wrong_operation", "plain_chat"])
def test_pair_route_and_structure_preconditions_cannot_be_bypassed(tmp_path, kind):
    snapshot = project(tmp_path)
    plan = pair(snapshot)["plan"]
    if kind == "plain_chat":
        case = plan.cases[1]
        request = case.request.model_copy(
            update={"input": case.request.input.model_copy(update={"response_format": "text"})}
        )
        plan = plan.model_copy(
            update={"cases": [plan.cases[0], case.model_copy(update={"request": request})]}
        )
    else:
        data = snapshot.config.model_dump(mode="json")
        if kind == "one_provider":
            data["aliases"]["response"].update(
                provider_ref="router", upstream_model="synthetic/responses"
            )
        else:
            data["aliases"]["response"]["operation"] = "chat"
            case = plan.cases[0]
            request = case.request.model_copy(
                update={
                    "operation": "chat",
                    "input": ChatInput(messages=[{"role": "user", "content": "public"}]),
                }
            )
            plan = plan.model_copy(
                update={"cases": [case.model_copy(update={"request": request}), plan.cases[1]]}
            )
        snapshot = load_project_config(write_config(tmp_path, data), check_credentials=False)
    with pytest.raises(PreflightError, match="parallel_smoke_requires"):
        preflight(plan, snapshot, enabled=True, approval=approval_for(plan, snapshot))


@pytest.mark.parametrize("both", [False, True])
def test_partial_or_unavailable_pair_never_becomes_a_one_route_plan(tmp_path, both):
    snapshot = project(tmp_path)
    result = propose_paid_smoke_pair(
        snapshot,
        principal_id="test",
        run_id="run-one",
        direct_alias="missing",
        router_alias="missing" if both else "summary",
        total_ceiling_usd="1",
    )
    assert result["plan"] is None and result["execution_authorized"] is False
    assert result["blocked_cases"][-1] == {
        "case_id": "concurrent_provider_pair",
        "reason": "parallel_smoke_requires_both_routes",
    }


def test_concurrent_execution_still_requires_separate_final_paid_enablement(tmp_path):
    snapshot = project(tmp_path)
    plan = pair(snapshot)["plan"]

    class Forbidden:
        def __getattr__(self, name):
            pytest.fail("unapproved parallel executor accessed gateway")

    report = run_plan(
        plan,
        snapshot,
        Forbidden(),
        enabled=True,
        approval=approval_for(plan, snapshot),
    )
    assert report["status"] == "unrun_precondition"
    assert report["precondition"] == "paid_execution_waits_for_final_owner_approval"
    assert report["inference_posts_attempted"] == 0
