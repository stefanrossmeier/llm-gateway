# Project configuration

Configuration belongs to the consuming project. The release ships reusable OpenAI
and OpenRouter provider definitions. Model aliases, credentials and deployment
inventory remain project-owned. Resolved schema:
[ProjectConfig](api/ProjectConfig.schema.json).

For bundled provider definitions, local provider/model alias files and configurable
endpoints/authentication, see [provider definitions](PROVIDERS.md).

Generate an executable fake example using `scripts/create_fake_project.py` as
shown in the [quickstart](../QUICKSTART.md). Pass each config explicitly:

```sh
llm-gateway config validate --project-config /absolute/private/project.yaml
llm-gateway serve --project-config /absolute/private/project.yaml   --state-dir /absolute/private/state --host 127.0.0.1 --port 8080
```

A project defines `schema_version`, `project_id`, `mode`, `timezone`, `providers`,
`aliases`, `access`, optional budgets/operation policies and evidence retention.
Providers select the `openrouter`, `openai` or `openai_chat` protocol adapter.
Bearer providers have an explicit credential reference; the generic chat adapter
also supports anonymous authentication. Requests cannot override hosts or credentials.
An alias selects provider, upstream model, operation, capabilities, limits and
reviewed pricing. Model changes require capability and price validation.

For paid aliases, configure positive call and run caps. Add a durable day/month
cap for long-lived service principals. Use project scope for a combined project
ceiling; alias scope provides a separate ceiling per alias. Policies overlap and
their reported debits must not be added together.

A principal can bind a fixed `run_id`, or set `allow_client_run_ids: true` and let
clients supply distinct durable run IDs. These settings are mutually exclusive.
Dynamic run authority requires a persistent day/month cap on every paid alias.
Do not use one permanent run ID to represent independent scheduled jobs.

Configuration reload creates an immutable snapshot. Existing request IDs keep
their original configuration. New requests may require an expected digest using
`X-Gateway-Expected-Config-Digest`; a mismatch fails before admission.

Enable `evidence_policy.retain_results: true` and a suitable
`result_ttl_seconds` when interrupted clients need to recover response content.
Prompt retention and result caching are separate explicit choices. Protect all
retained data and backups. See [X pricing](X_SEARCH.md) for live search fields.
