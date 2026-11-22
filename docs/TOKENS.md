# Gateway authentication

Create a token in a private directory outside the source checkout:

```sh
install -d -m 700 /absolute/private/tokens
llm-gateway token create --file /absolute/private/tokens/runner.token
```

Reference the absolute token path under `access.principals`. Grant only required
aliases, operations and permissions. Give application tokens `infer` and
`receipts:own`; use a separate operator token for `usage:project` and administrative
permissions when appropriate. HTTP loopback is not an authentication exception.

For long-lived job runners, use `allow_client_run_ids: true` with persistent
calendar caps. A fixed run grant is intended for one bounded logical run, not
all future executions of a service.

To rotate, use `token create --file PATH --rotate`, update mounted token files
and reload the project configuration. Validate readiness from the client. Tokens
and provider credentials are different secrets and should be rotated separately.
Never put tokens in URLs, Git, image layers, build arguments or public logs.
