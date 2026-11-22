# Operations

Use one independently owned configuration/state/secrets boundary per project.
The templates under `deploy/` are parameterized examples, not a record of any
installation. Pin the exact reviewed release image identity.

For authenticated TCP, use `deploy/compose.tcp.yaml` with an explicit private
environment file defining `LLM_GATEWAY_IMAGE`, `PROJECT_CONFIG_DIR`,
`PROJECT_SECRET_DIR`, `PROJECT_TOKEN_DIR`, `PROJECT_STATE_DIR`, optional loopback
`GATEWAY_BIND_PORT` and network alias `GATEWAY_HOSTNAME`. Run
`scripts/deploy_project.sh /absolute/private/gateway.env unique-project`.
Attach authorized clients to that project's internal `client` network.
Docker can omit host port publishing for an internal-only bridge even when a
loopback binding is requested. Verify actual bindings; do not assume that the
optional host port is reachable. The supported client path is the gateway's
network alias on its internal client network. A Linux host can use the inspected
bridge address, or an operator can use an authenticated request through an
attached client. Keep the inference container isolated; adding an unrestricted
network to make a port work would bypass that boundary. Provider
secrets and tokens must be readable by UID 10001; writable state must be owned
by UID 10001 and mode 700. The inference container has no outbound network; its
allowlisted CONNECT proxy is the sole outbound path.

Run as UID/GID 10001, with a read-only root, all capabilities dropped,
`no-new-privileges`, bounded resources and private writable state. Mount config,
tokens and provider credentials read-only. Use the egress proxy and an internal
network for the inference service; only the proxy needs outbound connectivity.
Never mount a Docker socket, another application's checkout or unrelated secrets.

`GET /health` is inference-free liveness. `GET /ready` authenticates and validates
service/config readiness without buying inference. Configure health checks,
restart policy and shutdown grace periods. A green process status alone is not
acceptance. [Cost reporting](ACCOUNTING.md) should be part of routine inspection.

## Upgrades

1. Verify release hashes, exact artifact security assessment and source identity.
2. Back up private state and configuration using the currently installed version.
3. Validate new configuration; preserve stable IDs, measured costs and unresolved holds.
4. Install the reviewed image, retaining the same owned state and secrets.
5. Verify version, authenticated readiness, accounting continuity and restricted runtime.
6. Retain the previous image and backup for a reviewed rollback.

Host CLI environments must be reinstalled from the same reviewed release. In a
source-development environment use `uv sync --frozen --extra service --no-editable
--reinstall-package llm-gateway` after source changes, including before isolated
worker tests. Do not infer installed-code freshness from Git cleanliness.

## State and recovery

```sh
llm-gateway state backup --state-dir /absolute/private/state   --output /absolute/private/backups/new-checkpoint.sqlite
llm-gateway state restore --input /absolute/private/backups/new-checkpoint.sqlite   --state-dir /absolute/private/new-restored-state
```

Restores start fenced because external charges may have occurred after a backup.
Preserve the original ledger and provider evidence; resolve gaps before permitting
new spending. Do not replace a live ledger with a fresh database to clear a cap.
Backups and retained results may contain sensitive data; protect them accordingly.
