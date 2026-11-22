# Operations

Review Scope: full repository, representative money, authority, recovery and isolation paths  
Doc Status: MAINTAINED  
Last Updated: 2026-11-22T17:01:43Z  
Updated By: agent, using ai-craftkit archdoc  
Source Repository: [llm-gateway](https://github.com/stefanrossmeier/llm-gateway/tree/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/)  
Source Revision: [6d36e06bd378a0c7f2326bae904b4b30ac0613a8](https://github.com/stefanrossmeier/llm-gateway/commit/6d36e06bd378a0c7f2326bae904b4b30ac0613a8)  
Source Basis: CLI/server, Dockerfile, Compose templates, CI/security gates, ledger backup/recovery and operational guides

This is a repository evidence review. It describes supported behavior, not any live installation.
Claims below are **verified** by the cited source unless marked otherwise.

## Runtime and local verification

The service runs live provider operations or key-free fake fixtures. Both use the
same gateway admission/receipt model; fake execution is explicitly simulated.
Clients may run without service dependencies. Start with the
[key-free quickstart](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/QUICKSTART.md) and use the exact release you reviewed.

[src/llm_gateway/service/cli.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/cli.py) checks the service platform, then
[src/llm_gateway/service/server.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/server.py) takes an exclusive state lease, loads
configuration, recovers dispatch evidence, activates snapshots and starts TCP or
private Unix IPC. Shutdown stops admission, cancels owned work and preserves
uncertainty. `python -I` workers load the installed package, so source changes
require reinstalling it ([docs/TESTING.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/TESTING.md)).

| Verification | Expected meaning |
| --- | --- |
| `ruff check`, `ruff format --check` | Source style, not runtime acceptance |
| Default `pytest` plus coverage gate | Inference-denied fault/contract suite; every authored module at least 90% branches |
| `scripts/check_container.py --image IMAGE` | Fake inference, non-root/read-only isolation and clean shutdown |
| `scripts/check_egress.py --image IMAGE` | Two synthetic independent stacks and restricted outbound path |
| `scripts/probe_native_profile.py --image IMAGE --output NEW_FILE` | Eight mocked SDK calls and exact native/runtime/source identity; no paid inference |

Commands and requirements are defined by [.github/workflows/ci.yml](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/.github/workflows/ci.yml),
[docs/TESTING.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/TESTING.md) and [repository map](REPO_MAP.md).

## Configuration and supported deployment shape

Project YAML owns providers, credential references, model aliases, limits, pricing,
call/run/day/month budgets, access grants, operation policy and evidence retention.
The loader rejects unsafe YAML, validates bounded schemas and snapshots non-secret
policy. File credentials require private regular files. See
[docs/CONFIGURATION.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/CONFIGURATION.md), [src/llm_gateway/config/loader.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/config/loader.py) and
[src/llm_gateway/config/secrets.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/config/secrets.py).

The parameterized [deploy/compose.tcp.yaml](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/deploy/compose.tcp.yaml) pairs one gateway on an internal
client network with a separate allowlisted CONNECT proxy on an outbound network.
The gateway runs as UID/GID 10001 with a read-only root, dropped capabilities,
`no-new-privileges` and bounded resources. The proxy receives no provider credentials.
The Unix-socket template is [deploy/compose.yaml](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/deploy/compose.yaml). These are supported
patterns, not an installation inventory.

Private TCP inputs are `LLM_GATEWAY_IMAGE`, `PROJECT_CONFIG_DIR`,
`PROJECT_SECRET_DIR`, `PROJECT_TOKEN_DIR`, `PROJECT_STATE_DIR`, optional
`GATEWAY_BIND_PORT` and `GATEWAY_HOSTNAME`. `scripts/deploy_project.sh` takes an
explicit private environment file and unique Compose project name. Writable state
must be owned by the service UID, mode 700; credential/token files must be private
and readable by that UID. Actual paths and secrets are operator-owned and excluded
from public source. See [docs/OPERATIONS.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/OPERATIONS.md).

## Health, cost and diagnostics

| Signal | Producer | Interpretation |
| --- | --- | --- |
| `/health` | API | Process liveness only; no inference |
| Authenticated `/ready` | API/engine | Version, config and readiness; no paid provider validation |
| `/v1/usage` | API/ledger | Known measured cost, estimates, protected exposure and uncertainty |
| Request/attempt receipts | Engine/ledger | Durable identity, routing/model evidence and execution/output/billing status |
| Safe startup/error codes | CLI/API | Diagnose without publishing raw provider errors, credentials or prompts |
| Security workflow reports | CI | Candidate collection versus strict exact-release review; these are different gates |

Evidence: [src/llm_gateway/service/api.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/api.py),
[src/llm_gateway/service/cli.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/service/cli.py), [docs/ACCOUNTING.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/ACCOUNTING.md).
**Missing:** a built-in metrics/dashboard or documented load/availability SLO;
operators must define monitoring appropriate to their own installation.

## Upgrade and failure recovery

| Situation | Supported handling |
| --- | --- |
| Transport response lost | Recover the original request ID; do not invent a new ID to retry spending |
| Content expired/not retained | Receipts survive; 410 does not authorize another inference |
| Unknown dispatch charge | Keep the hold; reconcile only with verified provider evidence |
| X allowance exceeded | Preserve measured charge and project fence; resolve unknowns, review policy and explicitly acknowledge with admin authority |
| Incompatible old capability snapshot | Stop admission; install a version supporting that historical revision; never rewrite receipt/snapshot identities |
| Backup restored with possible external billing gap | Restore starts fenced; review/reconcile the gap before spending |

Take a consistent private backup using the stopped/exclusively owned state CLI,
preserve configuration and the previous image, verify the next artifact's hashes,
then verify authenticated readiness and accounting continuity after upgrade.
Historical gateway-v1 through v4 snapshot reading is regression-tested in
[tests/offline/test_decision_pricing.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/tests/offline/test_decision_pricing.py); the current revision remains required
for new admissions. Backup/restore is implemented in
[src/llm_gateway/storage/backup.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/src/llm_gateway/storage/backup.py) and explained in
[docs/OPERATIONS.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/OPERATIONS.md). Never clear budgets by deleting a ledger.

## Release and security evidence

[docs/RELEASING.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/RELEASING.md) defines the reviewed artifact flow. Versioned manifests
bind source, wheel, frozen dependencies, both Linux image archives, SBOMs, raw scans
and native profiles. [scripts/check_release_assets.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/scripts/check_release_assets.py) verifies bytes before
execution. The security workflow scans exact released archives and rejects new,
expired or mismatched findings; a newly rebuilt image cannot inherit approval.

Raw OS matches remain visible. Dated restricted-profile dispositions are neither
patched-component claims nor a generally clean image scan. Candidate evidence
collection does not grant release approval. See
[.github/workflows/security.yml](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/.github/workflows/security.yml) and
[scripts/check_image_advisories.py](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/scripts/check_image_advisories.py). Private vulnerability reports use the
route documented in [SECURITY.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/SECURITY.md).

**Limits:** this static review does not establish live provider availability,
representative load performance or an installation's current state. Paid tests
require a separate exact-scope plan. X allowances are risk acceptance, not hard
upstream spending caps ([docs/X_SEARCH.md](https://github.com/stefanrossmeier/llm-gateway/blob/6d36e06bd378a0c7f2326bae904b4b30ac0613a8/docs/X_SEARCH.md)).
