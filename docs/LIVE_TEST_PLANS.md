# Live test plans

The testing package separates offline, explicitly enabled free and approved paid
checks. A live plan binds project/principal/run/config identity, cases, invocation
IDs, attempts and pricing. Preflight validates scope and persistent allowance
before execution. Reports preserve measured, estimated and unknown charges.

The bounded paid-plan helper only authorizes cases whose ceilings it can justify.
It must not label native X's risk allowance as a guaranteed ceiling. Verify X
separately under explicit operator risk acceptance and a one-request/no-retry
scope. Retain receipts even if schema/scope validation fails.

Use the CLI help for installed options:

```sh
python -m llm_gateway.testing.run --help
```

Free checks require `LLM_GATEWAY_ENABLE_FREE_TESTS=1` and `--run-live-free`.
Paid pytest groups require `--run-live-paid` and an explicit private approval
record. Do not commit live configuration, credentials, detailed prompt/response
reports or ledgers. Offline success does not establish current model availability.
