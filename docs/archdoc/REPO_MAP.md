# Repository map

Review Scope: full repository, representative money, authority, recovery and isolation paths  
Doc Status: MAINTAINED  
Last Updated: 2026-11-22T17:01:43Z  
Updated By: agent, using ai-craftkit archdoc  
Source Repository: [llm-gateway](https://github.com/stefanrossmeier/llm-gateway/tree/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/)  
Source Revision: [6d36e06bd378a0c7f2326bae904b4b30ac0613a8](https://github.com/stefanrossmeier/llm-gateway/commit/6d36e06bd378a0c7f2326bae904b4b30ac0613a8)  
Source Basis: README, package/lock, source exports, service entry points, tests, CI and deployment templates

This is a repository evidence review. It describes supported behavior, not any live installation.
Claims below are **verified** by the cited source unless marked otherwise.

## Orientation

LLM Gateway is a Python package containing a lean typed client and an optional,
stateful inference service. The service owns routing, admission, accounting and
provider execution; host applications retain their own domain workflows.
See [architecture](ARCHITECTURE.md), [API surface](API_SURFACE.md) and
[operations](OPERATIONS.md).

| Path | What to find there |
| --- | --- |
| [pyproject.toml](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/pyproject.toml) and [uv.lock](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/uv.lock) | Package, service extra, Python limits, CLI, frozen dependency graph and test configuration |
| [contracts/](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/contracts/__init__.py) | Public request/result/receipt types and exact USD helpers |
| [client/](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/client/__init__.py) | Sync/async HTTP clients and restricted OpenAI-compatible transports |
| [config/](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/config/loader.py) | Bounded YAML loading, immutable snapshots, provider/alias/budget/access policy |
| [service/](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/engine.py) | Orchestration, authorization, API translation, validation and lifecycle |
| [providers/](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/providers/runtime.py) | Preparation, pricing, owned worker and reviewed SDK transport |
| [storage/](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/storage/sqlite.py) | Schema/migrations, money ledger, leases, retained evidence and backup |
| [isolation/](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/isolation/process.py) | Bounded isolated subprocesses and network restrictions |
| [testing/](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/testing/runner.py) | Explicit live-test planning/preflight/runner support |
| [docs/api/](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/api/openapi.json) | Generated HTTP and JSON Schema contracts |
| [scripts/](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/scripts/check_coverage.py) | Schema export, verification, artifact review and generic deployment helpers |
| [deploy/](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/deploy/compose.tcp.yaml) and [Dockerfile](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/Dockerfile) | Parameterized runtime examples and reviewed container build |
| [.github/workflows/](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/.github/workflows/ci.yml) | Lean-client, service, publication and security gates |
| [release/](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/release/0.1.0a2.json) and [docs/evidence/](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/evidence/OS_ADVISORY_REVIEW_2026-11-22.md) | Versioned artifact manifests and dated security evidence; assess currency before use |

## Entry points and commands

The installed `llm-gateway` command enters
[service.cli:main](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/cli.py); `serve` creates the
owned service through [server.serve](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/server.py).
Library consumers import `llm_gateway.client` and `llm_gateway.contracts`.

| Command | Scope and evidence |
| --- | --- |
| `uv sync --frozen --extra service --no-editable --reinstall-package llm-gateway` | Service development environment; isolated workers need the installed authored code |
| `.venv/bin/pytest -q --cov=llm_gateway --cov-branch --cov-report=json:coverage.json` | Default inference-denied regression suite |
| `.venv/bin/python scripts/check_coverage.py coverage.json` | Every authored module at least 90% branch coverage, no exclusions |
| `.venv/bin/python scripts/export_schemas.py` | Regenerate canonical schemas/OpenAPI |
| `uv build --wheel --no-build-isolation` | Wheel from the reviewed build graph |
| `python3 scripts/check_container.py --image IMAGE` | Local Docker fake-provider acceptance; no paid inference |

Commands are established in [docs/TESTING.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/TESTING.md) and
[.github/workflows/ci.yml](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/.github/workflows/ci.yml). Start the service using the
[key-free quickstart](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/QUICKSTART.md), which creates private temporary fake state.
`dist/`, coverage outputs, virtual environments and runtime state are local artifacts;
only intentional versioned evidence belongs in Git ([.gitignore](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/.gitignore)).

## Tests and reading order

The default suite under `tests/offline/` covers contracts, SQLite money races,
authority, recovery, malformed output, process/network boundaries and release gates.
[Captured compatibility fixtures](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/tests/fixtures/consumers) are synthetic.
Lean-client checks separately run on Python 3.11 and 3.12. Live-free and paid test
markers require their own explicit preflight; offline success proves neither provider
availability nor production load capacity ([docs/TESTING.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/TESTING.md)).

Read the quickstart, [architecture](ARCHITECTURE.md), then the relevant API or
operations document. Detailed product design remains in
[docs/SPECIFICATION.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/SPECIFICATION.md) and [docs/IMPLEMENTATION_PLAN.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/IMPLEMENTATION_PLAN.md).
No separate accepted ADR directory or load/SLO benchmark was found in this review.
