import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from test_engine import ProviderDouble, configured

from llm_gateway.contracts import InferenceRequest, TokenUsage
from llm_gateway.providers.runtime import ProviderEvidence
from llm_gateway.service.engine import EngineError
from llm_gateway.service.output import normalize
from llm_gateway.service.x_scope import audit, subset_identity

pytestmark = pytest.mark.offline


def search(identifier="one", handles=None):
    today = datetime.now(UTC).date()
    return InferenceRequest.model_validate(
        dict(
            request_id=identifier,
            alias="summary",
            operation="x_search",
            context={"run_id": "run-one"},
            input={
                "kind": "x_search",
                "input": "public",
                "allowed_x_handles": handles or ["first", "second"],
                "from_date": str(today - timedelta(days=1)),
                "to_date": str(today),
                "response_schema": {
                    "type": "object",
                    "properties": {"posts": {"type": "array"}},
                    "required": ["posts"],
                },
            },
        )
    )


def payload(req, posts=None):
    posts = (
        posts
        if posts is not None
        else [
            {
                "author_handle": h,
                "post_url": f"https://x.com/{h}/status/{index + 1}",
                "post_text": "public",
            }
            for index, h in enumerate(req.input.allowed_x_handles)
        ]
    )
    query = (
        "("
        + " OR ".join("from:" + h for h in req.input.allowed_x_handles)
        + f") since:{req.input.from_date} until:{req.input.to_date}"
    )
    return {
        "status": "completed",
        "output": [
            {"type": "web_search_call", "action": {"query": query}},
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"posts": posts}),
                        "annotations": [
                            {"type": "url_citation", "url": p["post_url"]} for p in posts
                        ],
                    }
                ],
            },
        ],
    }


def mutate(data):
    profile = data["aliases"]["summary"]
    profile.update(
        operation="x_search",
        capabilities=["native_x_search", "json_schema"],
        operation_policy_ref="search",
    )
    profile["limits"].update(max_input_bytes=8192, max_input_tokens=16384, max_tool_calls=1)
    profile["pricing"].update(
        x_post_usd="0.005",
        x_profile_usd="0.01",
        x_request_allowance_usd="0.25",
        accept_unbounded_x_charges=True,
    )
    data["evidence_policy"] = {
        "retain_results": True,
        "result_ttl_seconds": 3600,
        "cache_enabled": True,
    }
    data["operation_policies"] = {
        "search": {
            "kind": "x_search",
            "allowed_x_handles": ["first", "second"],
            "max_handles": 2,
            "max_utc_days": 2,
            "max_posts": 6,
            "max_post_characters": 4000,
            "max_posts_per_author": 2,
            "provider_order": ["xAI"],
            "allowed_domains": ["x.com", "twitter.com"],
            "allow_subset_cache_reuse": True,
        }
    }


class XDouble(ProviderDouble):
    def __init__(self, body):
        super().__init__()
        self.body = body

    def invoke(self, prepared, provider, **kwargs):
        self.calls.append(prepared)
        return ProviderEvidence(
            copy.deepcopy(self.body),
            TokenUsage(input_tokens=5, output_tokens=2),
            Decimal("0.001"),
            "fixture",
            "xAI",
            "gen-fixture",
        )


def x_engine(tmp_path, body, monkeypatch):
    # Synthetic offline tariff ONLY: exercise scope/quota/ledger transitions
    # against XDouble. This is not a bound on the current provider's item fees.
    from llm_gateway.service import engine as engine_module

    original = engine_module.reserve_usd
    monkeypatch.setattr(
        engine_module,
        "reserve_usd",
        lambda profile, inputs, outputs, provider_kind=None: (
            Decimal("0.05")
            if profile.operation == "x_search"
            else original(profile, inputs, outputs, provider_kind=provider_kind)
        ),
    )
    engine, authority, snapshot = configured(tmp_path, mutate=mutate, provider=XDouble(body))
    grants = authority.grants.model_copy(update={"operations": ["x_search"]})
    return engine, type(authority)(authority.project, authority.principal, grants), snapshot


@pytest.mark.parametrize(
    "kind",
    [
        "no_search",
        "extra_search",
        "broad_query",
        "wrong_date",
        "wrong_author",
        "no_citation",
        "external_citation",
        "unsupported_tool",
    ],
)
def test_scope_failures_keep_measured_cost_and_daily_slot(tmp_path, kind, monkeypatch):
    req = search()
    body = payload(req)
    if kind == "no_search":
        body["output"].pop(0)
    elif kind == "extra_search":
        body["output"].insert(0, copy.deepcopy(body["output"][0]))
    elif kind == "broad_query":
        body["output"][0]["action"]["query"] += " OR news"
    elif kind == "wrong_date":
        body["output"][0]["action"]["query"] = body["output"][0]["action"]["query"].replace(
            str(req.input.to_date), "2000-01-01"
        )
    elif kind == "wrong_author":
        body["output"][0]["action"]["query"] = body["output"][0]["action"]["query"].replace(
            "from:first", "from:other"
        )
    elif kind == "no_citation":
        body["output"][1]["content"][0]["annotations"] = []
    elif kind == "external_citation":
        body["output"][1]["content"][0]["annotations"][0]["url"] = (
            "https://evil.invalid/first/status/1"
        )
    else:
        body["output"].insert(0, {"type": "function_call", "name": "execute"})
    engine, authority, _ = x_engine(tmp_path, body, monkeypatch)
    result = engine.run(authority, req)
    assert result.output_status != "valid" and result.provider_metadata["scope_verified"] is False
    assert result.receipts[0].measured_usd == Decimal("0.001")
    with pytest.raises(EngineError, match="quota_consumed"):
        engine.run(authority, search("two"))
    assert len(engine.provider.calls) == 1


def test_verified_empty_search_and_same_day_subset_reuse(tmp_path, monkeypatch):
    req = search()
    engine, authority, snapshot = x_engine(tmp_path, payload(req), monkeypatch)
    first = engine.run(authority, req)
    assert first.provider_metadata["scope_verified"]
    subset = engine.run(authority, search("two", ["first"]))
    assert subset.output_status == "valid"
    assert [p["author_handle"] for p in subset.content["posts"]] == ["first"]
    assert subset.receipts[0].evidence_status == "cached_reuse"
    assert subset.provider_metadata["original_scope"]["handles"] == ["first", "second"]
    assert subset.provider_metadata["x_scope"]["handles"] == ["first"]
    assert len(engine.provider.calls) == 1
    assert (
        engine.results.cached(
            "project-a", "owner", req, snapshot, now=datetime.now(UTC) + timedelta(days=1)
        )
        is None
    )
    empty = payload(req, [])
    profile, policy = (
        snapshot.config.aliases["summary"],
        snapshot.config.operation_policies["search"],
    )
    result = audit(
        empty,
        normalize(empty, req.input, profile),
        req.input,
        profile,
        policy,
        quota_period="fixture",
        request_identity=subset_identity(req),
    )
    assert result.status == "valid" and result.content == {"posts": []}


def test_native_x_billing_boundary_splits_by_provider_wire(tmp_path):
    # Only the supported OpenRouter wire may dispatch, under an explicit
    # risk allowance rather than a false per-call billing bound.
    provider = XDouble(payload(search()))

    def openai_kind(data):
        mutate(data)
        data["providers"]["router"]["kind"] = "openai"

    from llm_gateway.config import ConfigError

    # Provider definitions now reject unsupported operations at configuration
    # loading, before creating an engine or dispatching a provider request.
    with pytest.raises(ConfigError, match="configuration_schema_invalid"):
        configured(tmp_path, mutate=openai_kind, provider=provider)
    assert provider.calls == []

    provider2 = XDouble(payload(search()))
    engine2, authority2, _2 = configured(tmp_path, mutate=mutate, provider=provider2)
    authority2 = type(authority2)(
        authority2.project,
        authority2.principal,
        authority2.grants.model_copy(update={"operations": ["x_search"]}),
    )
    result = engine2.run(authority2, search("two"))
    assert result.receipts[0].reservation_kind == "risk_allowance"
    with engine2.store.transaction() as connection:
        connection.execute(
            "UPDATE attempts SET route_json=json_remove(route_json, '$.reservation_kind')"
        )
    historical = engine2.receipts(authority2, "two")
    assert historical[0].reservation_kind == "legacy_unclassified"
    assert result.execution_status == "completed"
    assert result.output_status == "valid"
    assert result.provider_metadata["scope_verified"] is True
    with engine2.store.connection() as db:
        assert db.execute("SELECT count(*) FROM quota_slots").fetchone()[0] == 1


def test_reasoning_items_are_ignored_and_subset_followup_queries_verify(tmp_path):
    """The OpenRouter wire emits empty reasoning items and refines the combined
    query with follow-up searches over handle subsets (observed live on grok);
    both stay inside the granted scope and must verify."""
    req = search("reason")
    body = payload(req)
    # Real live shape: reasoning items interleaved with a subset follow-up.
    body["output"] = [
        {"type": "reasoning", "summary": []},
        body["output"][0],
        {"type": "reasoning", "summary": []},
        {
            "type": "web_search_call",
            "action": {
                "query": (f"from:second since:{req.input.from_date} until:{req.input.to_date}")
            },
        },
        body["output"][-1],
    ]

    def fanout(data):
        mutate(data)
        # The OpenRouter wire reports every provider-side search as its own
        # web_search_call; the alias limit must allow the fan-out.
        data["aliases"]["summary"]["limits"]["max_tool_calls"] = 16

    provider = XDouble(body)
    engine, authority, _ = configured(tmp_path, mutate=fanout, provider=provider)
    authority = type(authority)(
        authority.project,
        authority.principal,
        authority.grants.model_copy(update={"operations": ["x_search"]}),
    )
    result = engine.run(authority, req)
    assert result.execution_status == "completed"
    assert result.output_status == "valid"
    assert result.provider_metadata["scope_verified"] is True
    assert result.provider_metadata["search_call_count"] == 2
    assert result.provider_metadata["x_search_queries"][1].startswith("from:second")


def test_live_redirect_citations_verify_with_field_author_scope(tmp_path):
    """The OpenRouter wire cites the /i/status/<id> redirect shape (observed
    live on grok); those citations carry no author, so the author scope is
    enforced on the post document while the citation backs the status id."""
    req = search("redirect", ["first", "second"])
    posts = [
        {
            "author_handle": "first",
            "post_url": "https://x.com/i/status/100",
            "post_text": "public",
        },
        {
            "author_handle": "second",
            "post_url": "https://x.com/second/status/200",
            "post_text": "public",
        },
    ]
    body = payload(req, posts)
    # Rewrite annotations to the live redirect shape plus one canonical URL.
    body["output"][-1]["content"][0]["annotations"] = [
        {"type": "url_citation", "url": "https://x.com/i/status/100"},
        {"type": "url_citation", "url": "https://x.com/second/status/200"},
    ]

    def fanout(data):
        mutate(data)
        data["aliases"]["summary"]["limits"]["max_tool_calls"] = 16

    provider = XDouble(body)
    engine, authority, _ = configured(tmp_path, mutate=fanout, provider=provider)
    authority = type(authority)(
        authority.project,
        authority.principal,
        authority.grants.model_copy(update={"operations": ["x_search"]}),
    )
    result = engine.run(authority, req)
    assert result.execution_status == "completed"
    assert result.output_status == "valid"
    assert result.provider_metadata["scope_verified"] is True

    # A post claiming an allowed author without any backing citation fails.
    uncited = payload(req, [dict(posts[0], post_text="uncited")])
    uncited["output"][-1]["content"][0]["annotations"] = [
        {"type": "url_citation", "url": "https://x.com/i/status/999"}
    ]
    provider2 = XDouble(uncited)
    second = tmp_path / "second"
    second.mkdir(mode=0o700)
    engine2, authority2, _ = configured(second, mutate=fanout, provider=provider2)
    authority2 = type(authority2)(
        authority2.project,
        authority2.principal,
        authority2.grants.model_copy(update={"operations": ["x_search"]}),
    )
    # Distinct input text: the subset cache would otherwise replay engine
    # one's verified result (same handles/dates) instead of validating.
    uncited_req = search("uncited", ["first", "second"])
    uncited_req = uncited_req.model_copy(
        update={"input": uncited_req.input.model_copy(update={"input": "other"})}
    )
    result2 = engine2.run(authority2, uncited_req)
    assert result2.output_status == "invalid_document"
    assert result2.provider_metadata["scope_verified"] is False


def test_rejected_scope_names_the_rule_and_offending_value(tmp_path):
    """A rejected retrieval must say WHICH rule failed and show the offending
    value: the bare diagnostic cost a live re-run to interpret once already."""
    req = search("diagnostic")
    body = payload(req)
    outside = f"from:outsider since:{req.input.from_date} until:{req.input.to_date}"
    body["output"].insert(1, {"type": "web_search_call", "action": {"query": outside}})

    provider = XDouble(body)

    def fanout(data):
        mutate(data)
        data["aliases"]["summary"]["limits"]["max_tool_calls"] = 16

    engine, authority, _ = configured(tmp_path, mutate=fanout, provider=provider)
    authority = type(authority)(
        authority.project,
        authority.principal,
        authority.grants.model_copy(update={"operations": ["x_search"]}),
    )
    result = engine.run(authority, req)
    assert result.output_status == "invalid_document"
    assert result.provider_metadata["scope_verified"] is False
    assert result.provider_metadata["scope_rejection"] == "query_scope_invalid"
    assert "from:outsider" in result.provider_metadata["scope_rejection_detail"]


def test_the_same_post_cited_by_two_url_shapes_is_a_duplicate(tmp_path):
    """A status id identifies one post: citing it once as /<handle>/status/<id>
    and once as /i/status/<id> must not smuggle the same post in twice."""
    req = search("duplicate", ["first"])
    post = {
        "author_handle": "first",
        "post_url": "https://x.com/first/status/100",
        "post_text": "public",
    }
    body = payload(req, [post, dict(post, post_url="https://x.com/i/status/100")])
    body["output"][-1]["content"][0]["annotations"] = [
        {"type": "url_citation", "url": "https://x.com/first/status/100"},
        {"type": "url_citation", "url": "https://x.com/i/status/100"},
    ]
    provider = XDouble(body)
    engine, authority, _ = configured(tmp_path, mutate=mutate, provider=provider)
    authority = type(authority)(
        authority.project,
        authority.principal,
        authority.grants.model_copy(update={"operations": ["x_search"]}),
    )
    result = engine.run(authority, req)
    assert result.output_status == "invalid_document"
    assert result.provider_metadata["scope_rejection"] == "post_scope_or_citation_invalid"
