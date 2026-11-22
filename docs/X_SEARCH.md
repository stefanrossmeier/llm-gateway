# Native X search

The `x_search` operation and `/v1/x-search` structured dashboard wire support
OpenRouter's native X search. A project supplies an explicit handle/date policy,
model/provider routing and response schema. No wider web, user lookup, thread or
unsupported-tool output is silently accepted. Scope validation checks the audited
queries, citations and returned posts; validation failure does not erase charges.

## Correct billing model

Native X charges fetched posts and user profiles, in addition to cumulative
agentic tokens. It is not a flat fee per search. At review on 2026-11-22, provider
documentation lists USD 0.005 per fetched post and USD 0.01 per fetched profile.
Keep these as project-owned reviewed rates; do not assume they never change.
Sources: [OpenRouter](https://openrouter.ai/docs/guides/features/server-tools/web-search)
and [provider usage details](https://docs.x.ai/developers/tools/tool-usage-details).

Live X pricing requires `x_post_usd`, `x_profile_usd`, token rates,
`x_request_allowance_usd` and `accept_unbounded_x_charges: true`. The allowance is
reserved before dispatch and subject to call/run/day admission policies. It is
**accepted risk, not a guaranteed maximum provider charge**. Result limits,
observed search counts, timeouts and prompt instructions cannot establish a hard
bound on every billable fetch or cumulative token input.

Provider-reported cost is authoritative for settlement even when output is
invalid. If complete fetched-item counts and token usage are supplied, the
fallback estimate uses those measured counts and configured rates. Missing item
counts remain unknown; returned post count and search count are never substitutes.
Unknown charges retain the allowance. An overrun records actual cost and blocks
further project admission pending review. Use upstream account/key limits as an
additional independent control; their semantics are provider-specific.

## Policy and recovery

Require a finite handle list, UTC date window, maximum posts/text size, explicit
provider order, and one admitted attempt without fallback. Persist one request ID
per logical retrieval. After an ambiguous transport failure, query its request or
receipt; do not submit it under a new ID. Keep result retention long enough for
recovery. Scope, quota and cache identities remain project-local.

After an overrun, reconcile any unknown charges, reload a reviewed configuration
with an allowance covering the known charge, then use an `admin:reconcile` token
to POST `/v1/admin/receipts/{id}/acknowledge-allowance` with `config_digest`, a
unique `evidence_id`, and a review `reason`. This records an audit entry and never
changes the original reservation or measured cost. Restore and unrelated fences
are not removed.
