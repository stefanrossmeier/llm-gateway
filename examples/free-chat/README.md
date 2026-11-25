# Free inference through a released LLM Gateway

Type a prompt, send it to the gateway using the model alias `kilo-auto-free`,
and print the answer. Kilo is configured entirely in
[`gateway/providers/kilo.yaml`](gateway/providers/kilo.yaml); the model alias in
[`gateway/models/kilo-auto-free.yaml`](gateway/models/kilo-auto-free.yaml) binds
it to `kilo-auto/free`. The application knows only the model alias.

Requires Docker Compose and Python 3 for downloading/verifying the image. No
provider account, API key, local Python packages, or gateway build is needed.
The client runs in the same released image and uses its installed GatewayClient.

```sh
git clone --branch v0.1.0a4 https://github.com/stefanrossmeier/llm-gateway.git
cd llm-gateway/examples/free-chat
python3 prepare.py
docker compose --env-file .release.env up -d --wait
docker compose --env-file .release.env run --rm client "Explain what an API gateway does in one sentence."
```

Omit the prompt argument to enter it interactively. `prepare.py` verifies the
selected platform archive against the release manifest before loading it, then
pins Compose to its immutable local image ID. It never builds the checkout.

Only the egress proxy has outbound connectivity. The gateway and client share a
private Docker network; no host port is required. Initialization creates a
private gateway bearer token in a Docker volume. Anonymous provider access does
not disable gateway authentication. The ledger and results survive restarts.

[Kilo documents anonymous free access](https://kilo.ai/docs/gateway/authentication)
at 200 requests per hour per public IP. Availability and the model behind
`kilo-auto/free` can change. There are no gateway retries or paid fallback routes.
Quota/provider failures are reported as failures; wait before trying a new logical
request. Use public sample prompts: free routing may log prompts and outputs as
described in [Kilo's model documentation](https://kilo.ai/docs/gateway/models-and-providers).

The client prints the request ID to stderr before dispatch. To recover a lost
response, repeat the same prompt with `--request-id ID`; the gateway retains
results for one hour. A new normal invocation creates a new request ID. Keep
the original identity when investigating an uncertain outcome.

```sh
docker compose --env-file .release.env logs gateway
docker compose --env-file .release.env down
```

`down` preserves the token, ledger, and retained results. Use `down -v` only when
you intentionally want to discard this example's complete local state.
