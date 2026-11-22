# Project configuration

Configuration belongs to the consuming project. The runtime ships no production
model catalog, credentials or deployment inventory. Schema:
[ProjectConfig](api/ProjectConfig.schema.json).

Generate an executable fake example using `scripts/create_fake_project.py` as
shown in the [quickstart](../QUICKSTART.md). Pass each config explicitly:

```sh
llm-gateway config validate --project-config /absolute/private/project.yaml
llm-gateway serve --project-config /absolute/private/project.yaml   --state-dir /absolute/private/state --host 127.0.0.1 --port 8080
```

A project defines `schema_version`, `project_id`, `mode`, `timezone`, `providers`,
`aliases`, `access`, optional budgets/operation policies and evidence retention.
Providers support `kind: openrouter` and `kind: openai` concurrently. Each has an
explicit credential reference; requests cannot override hosts or credentials.
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
