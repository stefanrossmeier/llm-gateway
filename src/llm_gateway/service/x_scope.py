"""Audit observed native-tool scope. Model assurances are not search evidence."""

import hashlib
import json
import re
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from llm_gateway.config.loader import canonical_json

from .output import Output

# Providers parenthesize multi-account queries; single-account follow-up
# refinement searches arrive bare (observed live on the OpenRouter wire).
# Either shape is accepted; the handles and dates are validated separately.
QUERY = re.compile(
    r"\(?from:([A-Za-z0-9_]+)(?: OR from:[A-Za-z0-9_]+)*\)? "
    r"since:(\d{4}-\d{2}-\d{2}) until:(\d{4}-\d{2}-\d{2})",
    re.I,
)


def subset_identity(request):
    body = request.model_dump(mode="json", exclude={"request_id"})
    body["input"].pop("allowed_x_handles", None)
    return hashlib.sha256(canonical_json(body).encode()).hexdigest()


def post_identity(url, policy):
    """(url_author | None, status_id) for a cited post URL.

    The OpenRouter wire cites both canonical /<handle>/status/<id> URLs and
    the /i/status/<id> redirect shape (observed live); the latter carries no
    author handle, so the author scope check falls to the post document's
    author field for that shape.
    """
    if not isinstance(url, str):
        raise ValueError("citation_url_invalid")
    parsed = urlsplit(url)
    # The redirect shape is checked first: /i/status/<id> also matches the
    # canonical handle pattern with the pseudo-handle "i".
    redirect = re.fullmatch(r"/i/status/([0-9]+)", parsed.path)
    canonical = (
        None
        if redirect is not None
        else re.fullmatch(r"/([A-Za-z0-9_]+)/status/([0-9]+)", parsed.path)
    )
    if (
        parsed.scheme != "https"
        or parsed.hostname not in policy.allowed_domains
        or parsed.port
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (canonical is None and redirect is None)
    ):
        raise ValueError("citation_url_out_of_scope")
    if canonical is not None:
        return canonical[1].casefold(), canonical[2]
    return None, redirect[1]


class ScopeRejection(ValueError):
    """A scope rule rejected the provider output; carries the rule and evidence.

    The public result stays ``invalid_document`` (callers already handle it),
    but the diagnostic metadata names the rule and shows the offending value so
    a live wire-shape change can be diagnosed without a second paid request.
    """

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def validate_posts(document, request, policy, citations=None, *, schema_validation=True):
    if schema_validation:
        Draft202012Validator(request.response_schema).validate(document)
    if not isinstance(document, dict) or not isinstance(document.get("posts"), list):
        raise ScopeRejection("posts_document_required")
    posts = document["posts"]
    if len(posts) > policy.max_posts:
        raise ScopeRejection("post_count_limit", f"{len(posts)} > {policy.max_posts}")
    allowed, counts, seen = {h.casefold() for h in request.allowed_x_handles}, {}, set()
    # A status id identifies one post, whatever URL shape cited it; keying the
    # seen set on the URL author would let the same post in twice (canonical
    # and /i/ redirect shapes).
    cited_ids = {status_id for _, status_id in citations} if citations is not None else None
    for post in posts:
        if not isinstance(post, dict):
            raise ScopeRejection("post_invalid")
        author = post.get("author_handle")
        text = post.get("post_text")
        url_author, status_id = post_identity(post.get("post_url"), policy)
        field_author = author.lstrip("@").casefold() if isinstance(author, str) else None
        if (
            field_author is None
            or field_author not in allowed
            # Canonical URLs carry the author: it must match the document field.
            or (url_author is not None and url_author != field_author)
            or not isinstance(text, str)
            or not 0 < len(text) <= policy.max_post_characters
            # Every post must be backed by a real cited search result.
            or (cited_ids is not None and status_id not in cited_ids)
            or status_id in seen
        ):
            raise ScopeRejection(
                "post_scope_or_citation_invalid",
                f"author={field_author} url_author={url_author} id={status_id}",
            )
        seen.add(status_id)
        counts[field_author] = counts.get(field_author, 0) + 1
        if policy.max_posts_per_author and counts[field_author] > policy.max_posts_per_author:
            raise ScopeRejection("per_author_post_limit", field_author)


def audit(payload, output, request, alias, policy, *, quota_period, request_identity):
    if output.status != "valid":
        output.metadata["scope_verified"] = False
        return output
    try:
        calls, queries, citations = [], [], []
        for item in payload.get("output") or []:
            kind = item.get("type")
            if kind == "web_search_call":
                if item.get("status") not in {None, "completed", "complete"}:
                    raise ScopeRejection("search_not_completed", item.get("status"))
                calls.append(item)
                queries.append((item.get("action") or {}).get("query"))
            elif kind == "custom_tool_call":
                calls.append(item)
                if item.get("name") not in {"x_keyword_search", "x_semantic_search"}:
                    raise ScopeRejection("unsupported_tool", item.get("name"))
                arguments = item.get("arguments", {})
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                queries.append(arguments.get("query"))
            elif kind == "reasoning":
                # Reasoning items carry no candidate content and no citations;
                # providers emit them even with reasoning disabled (observed
                # live on the OpenRouter wire). Ignore them for scope checks.
                continue
            elif kind != "message":
                raise ScopeRejection("unsupported_output_item", str(kind))
            for content in item.get("content") or []:
                for annotation in content.get("annotations") or []:
                    if annotation.get("type") == "url_citation":
                        citations.append(annotation.get("url"))
        citations.extend(payload.get("citations") or [])
        if not 1 <= len(calls) <= alias.limits.max_tool_calls:
            raise ScopeRejection(
                "search_count_invalid", f"{len(calls)} calls, limit {alias.limits.max_tool_calls}"
            )
        allowed = {h.casefold() for h in request.allowed_x_handles}
        for query in queries:
            query = " ".join(query.split()) if isinstance(query, str) else ""
            match = QUERY.fullmatch(query)
            authors = re.findall(r"from:([A-Za-z0-9_]+)", query, re.I)
            # Providers refine the supplied combined query with follow-up
            # searches over subsets of the allowed accounts (observed live).
            # Every query must stay inside the granted scope: any non-empty
            # subset of the allowed handles over the exact requested window.
            if (
                not match
                or not authors
                or not {h.casefold() for h in authors} <= allowed
                or len(authors) != len({h.casefold() for h in authors})
                or match[2] != str(request.from_date)
                or match[3] != str(request.to_date)
            ):
                raise ScopeRejection(
                    "query_scope_invalid",
                    f"{query[:300]} (allowed={sorted(allowed)} "
                    f"window={request.from_date}..{request.to_date})",
                )
        # Cited identities: canonical URLs carry the author (checked against
        # the allowed accounts); /i/ redirect citations carry only the status
        # id, so their author scope is enforced on the post document instead.
        identities = {post_identity(url, policy) for url in citations}
        if any(author is not None and author not in allowed for author, _ in identities):
            raise ScopeRejection(
                "citation_author_out_of_scope",
                next(
                    f"{author}:{status_id}"
                    for author, status_id in identities
                    if author is not None and author not in allowed
                ),
            )
        validate_posts(output.content, request, policy, identities)
        output.metadata = {
            "scope_verified": True,
            "search_call_count": len(calls),
            "x_search_queries": queries,
            "citation_urls": list(dict.fromkeys(citations)),
            "x_scope": {
                "handles": sorted(allowed),
                "from_date": str(request.from_date),
                "to_date": str(request.to_date),
                "quota_period": quota_period,
                "subset_identity": request_identity,
            },
        }
        return output
    except ScopeRejection as rejection:
        return Output(
            "invalid_document",
            finish_reason=output.finish_reason,
            metadata={
                "scope_verified": False,
                "scope_diagnostic": "native_search_scope_unverified",
                "scope_rejection": rejection.code,
                "scope_rejection_detail": str(rejection.detail)[:300],
            },
        )
    except Exception as error:  # schema or provider shape the audit did not anticipate
        return Output(
            "invalid_document",
            finish_reason=output.finish_reason,
            metadata={
                "scope_verified": False,
                "scope_diagnostic": "native_search_scope_unverified",
                "scope_rejection": type(error).__name__,
                "scope_rejection_detail": str(error)[:300],
            },
        )
