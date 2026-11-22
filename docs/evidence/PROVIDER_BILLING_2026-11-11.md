# Provider billing review — 2026-11-11

> Historical source analysis, not current artifact approval. Current behavior is
> specified in [the product specification](../SPECIFICATION.md) and [X support](../X_SEARCH.md).

OpenRouter's `:online` variant activates its web plugin; stacking it with `:free`
does not remove search charges. The gateway rejects that variant in configuration
and preparation, including stacked/case variants, declared overrides, fallbacks
and old snapshots. Ordinary hyphenated names and direct OpenAI fine-tune model
identifiers are unaffected. [Primary provider documentation](https://openrouter.ai/docs/guides/features/plugins/web-search).

Native X pricing requires a separate bound review. A scalar fee per tool call is
not adequate evidence for item-based billing. No paid native-X acceptance has
been run; no approval is implied by an offline fixture.


The [xAI pricing page](https://docs.x.ai/developers/pricing) charges per fetched
post/profile, including parent/quoted posts. The documented
[X tool parameters](https://docs.x.ai/developers/tools/x-search) provide
handle/date/media controls, without a fetched-item billing ceiling.
[OpenRouter's server-tool documentation](https://openrouter.ai/docs/guides/features/server-tools/web-search)
describes `max_total_results`, which the gateway already sends, as a cumulative
result limit; native `max_uses` is only forwarded to Anthropic and `max_results`
is ignored for native search. That does not establish a per-request hard cap on
all fetched/billed X items and profiles. Inferring such a guarantee would be
unsafe. The previous scalar per-call tariff is therefore withdrawn for live X.

Capability revision `gateway-v3` rejects live native X before admission/quota
consumption and returns no per-call estimate. Fake and explicitly synthetic
flat-fee offline scope/ledger tests remain available. Historical v1/v2 receipts
can be read, but new calls require reloading a current snapshot. No paid test was
run. Live-X acceptance and affected migrations remain open pending a defensible
upstream bound and final separate owner-approved acceptance.
