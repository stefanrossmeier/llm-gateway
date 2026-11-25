# Architecture

Review Scope: full repository, representative money, authority, recovery and isolation paths  
Doc Status: MAINTAINED  
Last Updated: 2026-11-22T17:01:43Z  
Updated By: agent, using ai-craftkit archdoc  
Source Repository: [llm-gateway](https://github.com/stefanrossmeier/llm-gateway/tree/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/)  
Source Revision: [6d36e06bd378a0c7f2326bae904b4b30ac0613a8](https://github.com/stefanrossmeier/llm-gateway/commit/6d36e06bd378a0c7f2326bae904b4b30ac0613a8)  
Source Basis: requirements/specification, config registry, engine, ledger, provider/process boundaries and regression tests

This is a repository evidence review. It describes supported behavior, not any live installation.
Claims below are **verified** by the cited source unless marked otherwise.

## Purpose and boundary

The repository owns a reusable inference gateway and its client contracts. External
actors are calling applications, operators and remote model providers. Applications
own job identities and business processing; providers are authoritative for actual
charges and model responses. The gateway owns admission decisions, local evidence
and recovery. SDKs are implementation dependencies inside this boundary, not separate
application services. Evidence: [docs/REQUIREMENTS.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/REQUIREMENTS.md) and
[docs/SPECIFICATION.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/SPECIFICATION.md).

## Drivers, constraints and strategy

| Explicit driver | Implemented consequence | Evidence |
| --- | --- | --- |
| Independent project policy and cost ownership | Configured aliases, authenticated grants and project-scoped ledger/reporting | [docs/REQUIREMENTS.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/REQUIREMENTS.md), [src/llm_gateway/service/auth.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/auth.py) |
| Preserve money and identity through failure | Transactional reservations, durable dispatch intent and immutable config snapshots | [src/llm_gateway/storage/ledger.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/storage/ledger.py), [src/llm_gateway/config/registry.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/config/registry.py) |
| Restrict credentials and unsafe output | Selected provider credentials in owned workers; key-free bounded validation | [src/llm_gateway/providers/process.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/providers/process.py), [src/llm_gateway/service/validation.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/validation.py) |
| Lean client compatibility | Contracts/clients avoid service and SDK imports | [src/llm_gateway/client/__init__.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/client/__init__.py), [.github/workflows/ci.yml](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/.github/workflows/ci.yml) |
| Honest uncertain billing | Separate measured cost, estimates, holds and explicit X risk allowances | [src/llm_gateway/providers/pricing.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/providers/pricing.py), [docs/ACCOUNTING.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/ACCOUNTING.md) |

Hard constraints are Python 3.11–3.12 for the package, exactly Python 3.12 and POSIX
for the service, the pinned LiteLLM service dependency, private local SQLite and
reviewed provider protocols. See [pyproject.toml](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/pyproject.toml),
[src/llm_gateway/service/cli.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/cli.py) and [src/llm_gateway/storage/sqlite.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/storage/sqlite.py).
These constraints are observable contracts; their historical selection rationale is
not inferred from code.

## Building blocks and dependency direction

The client submits typed contracts. HTTP/compatibility handlers translate inputs
and authenticate before invoking the engine. The engine coordinates config snapshots,
ledger transitions, preparation, owned provider execution and isolated output checks.
Provider SDK calls occur in `providers/runtime.py`, not in HTTP handlers. Storage
and configuration are service concerns; the lean contract/client layer does not
need them. Evidence: [src/llm_gateway/service/api.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/api.py),
[src/llm_gateway/service/engine.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/engine.py), [src/llm_gateway/providers/runtime.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/providers/runtime.py)
and [tests/offline/test_contracts.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/tests/offline/test_contracts.py).

| State | Owner/authority | Lifetime and access |
| --- | --- | --- |
| Logical job/run/request IDs | Caller | Caller persists them; authenticated grants constrain their use |
| Provider cost and raw response evidence | Provider externally; captured evidence locally | Measured costs remain separate from inferred estimates |
| Config snapshots and active pointer | Gateway config registry | Immutable digest plus capability revision; transactional activation |
| Requests, attempts, holds, quota slots, receipts and runtime fences | Gateway ledger | Private durable SQLite, one exclusive service owner per state directory |
| Retained results/prompts and cache | Gateway, under project policy | Optional, expiring; recovery content depends on retention |
| Concurrency slots, cancellation and worker processes | Service process | Transient; restart reconciles durable dispatch state conservatively |

The schema and write paths are [src/llm_gateway/storage/sqlite.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/storage/sqlite.py),
[src/llm_gateway/storage/ledger.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/storage/ledger.py), [src/llm_gateway/storage/results.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/storage/results.py)
and [src/llm_gateway/storage/outbox.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/storage/outbox.py). This service is stateful; provider
invoices remain externally authoritative.

## Representative request and recovery

1. The API authenticates bearer or verified private Unix authority and normalizes a wire request.
2. The engine binds project/principal/request identity and an immutable config digest.
3. Preparation resolves a configured provider/model and computes a reservation or X allowance.
4. A ledger transaction admits against overlapping budgets and records dispatch intent.
5. An owned worker performs the provider call; billing evidence is preserved independently of output validity.
6. Key-free validation produces the result; the ledger settles or retains uncertainty.
7. A repeated identity looks up the original invocation; different semantic content conflicts.

These interactions are verified in [src/llm_gateway/service/engine.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/engine.py),
[tests/offline/test_engine.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/tests/offline/test_engine.py) and
[tests/offline/test_decision_pricing.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/tests/offline/test_decision_pricing.py). Detailed wire ownership is in
[API surface](API_SURFACE.md); restart, restore and reconciliation are in
[operations](OPERATIONS.md).

## Cross-cutting contracts and invariants

- Money uses decimal USD; overlapping budget balances are not additive spend. Admission and settlement are transactional ([src/llm_gateway/contracts/money.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/contracts/money.py), [src/llm_gateway/storage/buckets.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/storage/buckets.py)).
- Caller labels cannot grant project/principal/provider authority. A request cannot supply a destination or secret ([src/llm_gateway/service/auth.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/auth.py), [src/llm_gateway/providers/preparation.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/providers/preparation.py)).
- Execution, output validity and billing completeness are independent. Invalid output or cancellation does not establish a free call ([src/llm_gateway/contracts/models.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/contracts/models.py), [src/llm_gateway/service/engine.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/engine.py)).
- Configuration reload does not rewrite old receipt identities; historical gateway-v1 through v4 snapshots remain readable, while new calls require the current capability revision ([src/llm_gateway/config/loader.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/config/loader.py), [tests/offline/test_decision_pricing.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/tests/offline/test_decision_pricing.py)).
- Process input/output, wall time, queue/concurrency, request size and validation work are bounded. Owned children are reaped and receive scrubbed environments ([src/llm_gateway/isolation/process.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/isolation/process.py), [src/llm_gateway/service/validation_worker.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/validation_worker.py)).
- Native X fetched-item charges have no guaranteed upstream cap. Acknowledged risk allowance, measured counts/cost, unknown holds and overrun fencing replace a fabricated tool-count spending bound ([src/llm_gateway/providers/pricing.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/providers/pricing.py), [docs/X_SEARCH.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/X_SEARCH.md)).

## Decisions, risks and gaps

[docs/IMPLEMENTATION_PLAN.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/IMPLEMENTATION_PLAN.md) records design evolution and
[docs/SPECIFICATION.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/SPECIFICATION.md) records product contracts. **Missing:** a separate
accepted ADR set establishing historical human rationale for every mechanism.
Do not treat this generated description as an approval of new design decisions.

Since 0.1.0a4, [provider and model alias definitions](../PROVIDERS.md) can be loaded
from files. Endpoints, authentication and approved free routes are configuration;
the generic `openai_chat` adapter supports compatible services without code changes.
Snapshots capture resolved definitions and the proxy derives its destinations
from project configuration. Bundled provider data participates in source identity.

Known limits: external prices/capabilities can change; new protocols require
adapter and security review. SQLite/exclusive ownership is not a distributed multi-writer service.
Advisory dispositions apply to exact restricted artifacts and expire. **Missing:**
representative load/latency SLO evidence and a universal paid-provider acceptance
matrix. These do not follow from offline tests ([docs/TESTING.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/TESTING.md),
[docs/RELEASING.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/RELEASING.md)).
