# API surface

Review Scope: full repository, representative money, authority, recovery and isolation paths  
Doc Status: MAINTAINED  
Last Updated: 2026-11-22T17:01:43Z  
Updated By: agent, using ai-craftkit archdoc  
Source Repository: [llm-gateway](https://github.com/stefanrossmeier/llm-gateway/tree/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/)  
Source Revision: [6d36e06bd378a0c7f2326bae904b4b30ac0613a8](https://github.com/stefanrossmeier/llm-gateway/commit/6d36e06bd378a0c7f2326bae904b4b30ac0613a8)  
Source Basis: public exports, HTTP registrations, wire/contracts/reports, CLI parser, generated OpenAPI and boundary tests

This is a repository evidence review. It describes supported behavior, not any live installation.
Claims below are **verified** by the cited source unless marked otherwise.

## Contracts and ownership

Version 0.1.0a4 adds explicit local provider and model alias definition files and
bundled `builtin:openai` / `builtin:openrouter` definitions. The public callsite
still sends a model alias. `adapter`, HTTPS `base_url`, authentication and free
route declarations are gateway configuration. See [providers](../PROVIDERS.md)
for the loader input contract and resolved snapshot schema. Alias inspection also
reports the generic `openai_chat` protocol adapter.

The canonical machine-readable contracts are [OpenAPI](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/api/openapi.json)
and [InferenceRequest JSON Schema](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/api/InferenceRequest.schema.json).
The service HTTP owner is [create_app](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/api.py);
wire projections are [src/llm_gateway/service/wire.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/wire.py) and
[src/llm_gateway/service/compatibility.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/compatibility.py). See [architecture](ARCHITECTURE.md)
for boundaries and [operations](OPERATIONS.md) for diagnostics.

| Surface | Consumer and owner | Important contract |
| --- | --- | --- |
| `POST /v1/inferences` | Typed clients; API/engine | Stable request ID, configured alias, operation, context and typed input |
| `GET /v1/requests/{id}`, cancellation and receipt lookup | Recovery/reporting clients; engine/ledger | Original identity, snapshot and durable attempt evidence; principal scoping |
| `GET /v1/usage` | Cost consumers; API/ledger | Exact USD subtotals, uncertainty, grouping and time windows |
| `GET /health`, `GET /ready`, `GET /v1/aliases` | Operators/clients; API | Liveness, authenticated readiness/version/config and permitted routes |
| `/v1/chat`, `/v1/x-search`, `/chat`, `/decisions`, restricted OpenAI-shaped wires | Compatibility clients; compatibility/API | Translate into the same admission/accounting pipeline |
| `/v1/admin/reload`, receipt reconciliation and allowance acknowledgement | Explicitly privileged operators; API/ledger | Separate admin grants and reviewed evidence; no implicit authority |
| `llm_gateway.client`, `llm_gateway.contracts` | Host applications; public package exports | Sync/async typed clients, HTTP transports, request/result/receipt types |
| `llm-gateway config/token/serve/state` | Operators; CLI | Local policy validation, token provisioning, service ownership, backup/restore |

No event broker or public webhook contract was found in the reviewed interface registrations.
The alpha package is evolving; inspect the exact release schemas rather than assuming
arbitrary OpenAI SDK features or provider kwargs are supported.

## Authentication, identity and failure

All HTTP routes except `/health` require a configured bearer or verified private Unix
IPC authority. Grants determine aliases, operations, receipts, reporting and admin
permissions; body project labels are not authority. Evidence:
[src/llm_gateway/service/auth.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/auth.py) and [src/llm_gateway/service/api.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/api.py).

The canonical ID or `Idempotency-Key` identifies one invocation. Reuse the same
logical ID and semantic payload across recovery; changed content conflicts.
`X-Gateway-Expected-Config-Digest` can bind admission to an inspected configuration.
Lookup retains the original snapshot. Dashboard recovery uses
`GET /v1/requests/{id}?format=dashboard`; result replay requires retained content.
Evidence: [docs/API.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/API.md), [src/llm_gateway/service/api.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/api.py),
[src/llm_gateway/client/http.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/client/http.py).

Typical outcomes are 401 authentication, 403 authority, 409 conflicting identity or
configuration, 422 malformed/unsupported input, 202 pending and 410 unavailable
retained content. Consult OpenAPI and safe error codes for individual routes. A
non-success response may still have charged receipts. `execution_status`,
`output_status`, nullable usage and billing evidence must be interpreted separately
([src/llm_gateway/contracts/models.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/contracts/models.py)).

## Cost interface

`GET /v1/usage` supports alias/run/provider/model grouping plus day/month grouping,
`since` inclusive and `until` exclusive timezone-aware instants. Calendar grouping
uses the configured project timezone. `usage:project` permits the project view;
`receipts:own` restricts it. Reports do not select another project or leak shared
bucket balances to narrowly scoped callers.

`measured_usd`, `estimated_usd`, `protected_debit_usd` and
`contingent_exposure_usd` are decimal strings with different meanings.
`unknown_attempts`, `unsettled_attempts` and `usage_complete` expose missing evidence;
unknown cost is not zero. See [docs/ACCOUNTING.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/ACCOUNTING.md) and
[src/llm_gateway/contracts/reports.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/contracts/reports.py) for the complete semantics.

## Library and CLI

Public exports are explicit in [src/llm_gateway/client/__init__.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/client/__init__.py) and
[src/llm_gateway/contracts/__init__.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/contracts/__init__.py). `GatewayClient`/`AsyncGatewayClient`
provide typed service access. OpenAI-compatible transports require explicit gateway
invocation bindings; they are restricted adapters, not arbitrary remote-provider proxies.
Clients need HTTPX/Pydantic and Python 3.11 or 3.12, without the service extra.

The CLI parser ([src/llm_gateway/service/cli.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/cli.py)) defines:
`config validate --project-config`, `token create --file [--rotate]`, `serve`,
`state backup --state-dir --output` and `state restore --input --state-dir`.
Credential paths are private operator input; secret values never belong in command
arguments or public documentation. See [docs/TOKENS.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/TOKENS.md).

## Verification and remaining limits

[tests/offline/test_contracts.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/tests/offline/test_contracts.py), [tests/offline/test_client_boundaries.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/tests/offline/test_client_boundaries.py),
[tests/offline/test_http_admission_boundaries.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/tests/offline/test_http_admission_boundaries.py) and
[tests/offline/test_decision_pricing.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/tests/offline/test_decision_pricing.py) establish representative contract,
authority, failure and historical-snapshot behavior. Generated schemas are exported
by [scripts/export_schemas.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/scripts/export_schemas.py). **Missing:** a claim of live acceptance for
all provider/model/capability combinations; no such guarantee is made by these contracts.
