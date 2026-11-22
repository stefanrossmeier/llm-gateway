"""Scope validators remain enforced for fake/offline and historical X results."""

import copy

import pytest
from jsonschema.exceptions import ValidationError
from test_paid_proposal import project
from test_x_scope import search

from llm_gateway.service.output import Output
from llm_gateway.service.x_scope import audit, validate_posts

pytestmark = pytest.mark.offline


@pytest.mark.parametrize(
    "bad",
    [
        "document",
        "post_type",
        "too_many",
        "duplicate",
        "author",
        "text",
        "per_author",
        "url_type",
        "external_url",
    ],
)
def test_invalid_posts_never_become_verified_scope(tmp_path, bad):
    # Use the original synthetic two-handle policy; no provider or live tariff.
    from llm_gateway.config.models import XSearchPolicy

    policy = XSearchPolicy(
        kind="x_search",
        allowed_x_handles=["first", "second"],
        max_handles=2,
        max_utc_days=2,
        max_posts=6,
        max_post_characters=100,
        max_posts_per_author=1,
        provider_order=["xAI"],
        allowed_domains=["x.com"],
    )
    req = search().input
    document = {
        "posts": [
            {
                "author_handle": "first",
                "post_url": "https://x.com/first/status/1",
                "post_text": "public",
            }
        ]
    }
    if bad == "document":
        document = {"posts": "invalid"}
    elif bad == "post_type":
        document["posts"] = [42]
    elif bad == "too_many":
        document["posts"] *= 7
    elif bad == "duplicate":
        document["posts"] *= 2
    elif bad == "author":
        document["posts"][0]["author_handle"] = "second"
    elif bad == "text":
        document["posts"][0]["post_text"] = "x" * 101
    elif bad == "per_author":
        document["posts"].append(
            document["posts"][0] | {"post_url": "https://x.com/first/status/2"}
        )
    elif bad == "url_type":
        document["posts"][0]["post_url"] = 42
    else:
        document["posts"][0]["post_url"] = "https://evil.invalid/first/status/1"
    with pytest.raises((ValueError, ValidationError)):
        validate_posts(document, req, policy)


@pytest.mark.parametrize(
    "bad", ["unfinished", "unknown_native", "author_citation", "nonstring_query"]
)
def test_native_tool_audit_rejects_incomplete_or_unverifiable_execution(tmp_path, bad):
    config = project(tmp_path).config
    selected, policy = config.aliases["search"], config.operation_policies["scope"]
    from llm_gateway.contracts import XSearchInput

    req = XSearchInput(
        input="public",
        allowed_x_handles=["public_example"],
        from_date="2026-11-10",
        to_date="2026-11-11",
        response_schema={"type": "object"},
    )
    body = {
        "output": [
            {
                "type": "custom_tool_call",
                "name": "x_keyword_search",
                "arguments": {"query": "(from:public_example) since:2026-11-10 until:2026-11-11"},
            }
        ]
    }
    if bad == "unfinished":
        body["output"] = [{"type": "web_search_call", "status": "failed"}]
    elif bad == "unknown_native":
        body["output"][0]["name"] = "arbitrary_function"
    elif bad == "author_citation":
        body["citations"] = ["https://x.com/other/status/1"]
    else:
        body["output"][0]["arguments"]["query"] = 42
    result = audit(
        body,
        Output("valid", {"posts": []}),
        req,
        selected,
        policy,
        quota_period="fixture",
        request_identity="fixture",
    )
    assert result.status == "invalid_document" and not result.metadata["scope_verified"]


def test_supported_custom_native_tool_arguments_are_data_and_scope_verified(tmp_path):
    config = project(tmp_path).config
    from llm_gateway.contracts import XSearchInput

    req = XSearchInput(
        input="public",
        allowed_x_handles=["public_example"],
        from_date="2026-11-10",
        to_date="2026-11-11",
        response_schema={"type": "object"},
    )
    body = {
        "output": [
            {
                "type": "custom_tool_call",
                "name": "x_semantic_search",
                "arguments": '{"query":"(from:public_example) since:2026-11-10 until:2026-11-11"}',
            }
        ]
    }
    before = copy.deepcopy(body)
    result = audit(
        body,
        Output("valid", {"posts": []}),
        req,
        config.aliases["search"],
        config.operation_policies["scope"],
        quota_period="fixture",
        request_identity="fixture",
    )
    assert result.status == "valid" and result.metadata["scope_verified"]
    assert body == before
    prior_failure = audit(
        body,
        Output("refused"),
        req,
        config.aliases["search"],
        config.operation_policies["scope"],
        quota_period="fixture",
        request_identity="fixture",
    )
    assert prior_failure.status == "refused" and not prior_failure.metadata["scope_verified"]
