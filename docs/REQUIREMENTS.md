# Product requirements

The gateway exists to replace duplicated provider transports with shared,
reviewable behavior while keeping policy and state owned by each consumer.

- Explicit project aliases select operation, model, provider, limits and pricing.
- OpenAI and OpenRouter may coexist; a request cannot override destinations or keys.
- Typed canonical contracts cover chat, Responses/functions, embeddings, Decisions and X.
- Compatibility routes use the same admission, authorization and receipt pipeline.
- Decimal USD reservations are atomic across overlapping call/run/day/month policies.
- Measured cost, estimates and uncertainty stay distinct from output validity.
- Provider/search uncertainty is represented honestly; X requires explicit risk acceptance.
- Invocation identity survives process/transport failure and preserves its original config.
- Concurrent/cancelled/restarted work does not erase dispatch intent or uncertain charges.
- Project/principal authorization applies to inference, lookup, reporting and administration.
- Provider workers receive selected credentials; schema validation receives none.
- Runtime state is private local SQLite with exclusive service ownership and fenced restore.
- Defaults do not retain prompts; result retention/cache are explicit consumer policy.
- The client remains usable without service/provider SDK imports.
- Release evidence binds source, artifacts, dependency graph, runtime and advisory review.
- Public documentation contains no operational inventory or private consumer data.

Paid live provider availability and universal model capability are not inferred
from offline tests. Read [specification](SPECIFICATION.md) and [testing](TESTING.md).
