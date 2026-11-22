# HTTP API

All routes except `GET /health` require a configured bearer token, or verified
private Unix IPC authority. The generated [OpenAPI](api/openapi.json) and
[schemas](api/InferenceRequest.schema.json) describe canonical contracts.

| Route | Purpose |
| --- | --- |
| `GET /health` | Liveness; no provider call |
| `GET /ready` | Authenticated readiness and config digest |
| `GET /v1/aliases` | Authorized alias/capability inspection |
| `POST /v1/inferences` | Canonical typed inference |
| `GET /v1/requests/{id}` | Recover invocation and receipts; optional `format=dashboard` |
| `POST /v1/requests/{id}/cancel` | Request cancellation; charges may remain |
| `GET /v1/receipts/{id}` | Own durable attempt receipt |
| `GET /v1/usage` | Scoped [cost and exposure reporting](ACCOUNTING.md) |
| `POST /v1/admin/receipts/{id}/acknowledge-allowance` | Explicit review after a measured X allowance overrun |
| `POST /v1/admin/reload` | Reload configured project path |
| `POST /v1/admin/receipts/{id}/reconcile` | Reconcile verified billing evidence |

Compatibility inference routes include `/v1/chat`, `/v1/x-search`, `/chat`,
`/decisions` and the restricted OpenAI-shaped chat/Responses/embedding wires.
They delegate to the same authenticated admission/accounting pipeline. They do
not accept arbitrary provider kwargs, destinations, hidden retries or streaming.

Supply `Idempotency-Key` or the canonical body request ID. Conflicting identities
or changed semantic content are rejected. Optional
`X-Gateway-Expected-Config-Digest` binds new admission to an inspected snapshot.
Existing identities recover their original snapshot. Response headers identify
trace, receipt, config and execution/output status.

HTTP success does not prove complete billing measurement. Inspect receipt fields.
Non-200 results may include durable receipts. Authentication errors are 401,
authority errors 403, malformed/unsupported requests 422, pending results 202,
and unavailable retained results 410. Never log credentials, raw provider errors
or real prompts into public issue reports.
