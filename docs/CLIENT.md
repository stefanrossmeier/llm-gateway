# Clients and recovery

`GatewayClient` and `AsyncGatewayClient` provide typed canonical requests, usage,
readiness, alias and receipt inspection. Their imports do not initialize provider
SDKs. See the [quickstart](../QUICKSTART.md) for an executable fake example.

Persist one stable invocation ID before dispatch. Derive it from a durable job ID
and logical operation identity, or store it in the application's own run record.
Do not generate a process nonce for a recoverable paid invocation. Reuse the ID
when retrying a lost response; an intentional new logical run uses a new run ID.
Different operations within a run need different invocation IDs.

`GET /v1/requests/{request_id}` returns the canonical result and receipts.
`?format=dashboard` recovers the structured dashboard projection. A pending
request returns 202; expired/unretained content returns 410, while its accounting
remains inspectable. A terminal failure is not permission to spend under a new ID.

Dashboard bodies accept `run_id`; long-lived service principals may authorize
client-supplied IDs with persistent calendar caps. Authentication is required
for every HTTP route except liveness. Use TLS across untrusted networks.

Unknown usage/cost values are nullable. Consumers must preserve them through
validation, summaries and output, rather than filling them with invented zeros.
See [accounting](ACCOUNTING.md) for reporting filters and field meanings.
