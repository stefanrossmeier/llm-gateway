# Gateway specification

## Layers

`contracts` defines immutable validated request/result/receipt/report types.
`client` owns HTTP/Unix transport and scoped inspection. `config` validates
consumer-owned policy and freezes immutable snapshots. `storage` owns durable
admission, dispatch intent, exact settlement, quota, retention and recovery.
`providers` adapts the pinned SDK through owned HTTP boundaries. `isolation`
owns selected environment, network and process limits. `service` coordinates
those layers without moving provider I/O inside database transactions.

## Invocation lifecycle

Authenticate the project/principal; validate the public wire; resolve an immutable
alias/config; authorize capabilities and context. Register a stable invocation
identity and semantic fingerprint. Repeated identical IDs recover their existing
snapshot/result; conflicting content is rejected. Prepare the reviewed transport,
reserve against every applicable budget atomically, persist dispatch intent,
then perform one owned upstream attempt. Validate output independently from
billing evidence. Settle measured/estimated/unknown cost and persist the result
according to explicit retention policy. Retries require separate admitted attempts.

## Money and uncertainty

USD values are bounded exact decimals. Measured provider cost settles even if
output is unusable. Unknown dispatch outcomes preserve protected exposure. A
configured limit is not proof of upstream enforceability. Native X therefore uses
an explicitly accepted risk allowance and per-fetched-item accounting; it does
not invent a search-count charge ceiling. Reports expose known cost and
uncertainty separately. Budget buckets overlap and cannot be summed as spending.

## Scope

Principal grants constrain project aliases, operations, permissions and optional
fixed run/phase. Dynamic job runners explicitly authorize client run IDs and need
persistent calendar caps. HTTP authentication has no loopback bypass. Administrative
requests cannot select arbitrary source paths or mutate another project's state.
X policy bounds accepted handles, dates and output, validates auditable queries
and cited post identity, and preserves durable quota/cache identities.

## Isolation and recovery

The parent coordinates owned provider and key-free validation processes. Credentials
are selected per provider and scrubbed from unrelated startup variables. Typed
request data cannot choose arbitrary SDK hooks, tools, FFI, filesystem paths or
network endpoints. The Python socket guard supplements deployment egress controls;
it is not a general native-code sandbox. A stopped/interrupted process retains
billing uncertainty. Restored checkpoints begin fenced until external gaps are
reviewed. Released schema definitions are not edited in place.

## Supported boundaries

Service: Python 3.12/POSIX. Lean client: Python 3.11–3.12. Provider SDK: exact
reviewed LiteLLM pin. OpenAI-shaped wires are explicit subsets, not universal SDK
compatibility. Streaming, arbitrary provider plugins and user-executed local
commands are outside the public contract. Model/capability/price policy belongs
to consumers and needs revalidation when changed.
