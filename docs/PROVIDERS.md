# Provider definitions and model aliases

Applications call a gateway model alias. Its definition binds a provider
instance, upstream model, operation, capabilities, limits and pricing. The
application never supplies the provider URL or credentials.

## Explicit files

The project loader accepts inline entries or explicit definition references under
`providers` and `aliases`. Existing schema-version-1 inline projects remain valid.

```yaml
schema_version: 1
project_id: my-project
mode: live
providers:
  router:
    definition: builtin:openrouter
    credential:
      file_path: /secrets/openrouter.key
  custom:
    definition: providers/custom.yaml
aliases:
  gpt-example:
    definition: models/gpt-example.yaml
# Configure access/principals as described in TOKENS.md.
```

`builtin:openai` and `builtin:openrouter` select definitions shipped under
`llm_gateway/config/providers/` in the installed release. They contain no
credentials or application model aliases. Additional definitions are local
YAML/JSON files; compatible chat services need no Python plugin or code change.

Paths are relative to the project file's directory. Absolute paths, traversal,
remote URLs, recursive definitions, YAML aliases/merges and symlink components
are rejected. A file is limited to 1 MiB, all local definitions to 16 MiB, and a
project to 64 providers and 256 aliases. There is no directory discovery or network
retrieval. A provider reference may also supply `credential`, `enabled` and
`application_name`; no other override or implicit deep merge is allowed. Alias
references contain only `definition`; edit the alias file to change its policy.

## Provider schema

```yaml
adapter: openai_chat
base_url: https://inference.example/api/v1
authentication:
  type: bearer
operations: [chat]
free_models: [example/free]
free_model_suffixes: []
```

`adapter` selects protocol implementation, not provider identity. `openai_chat`
implements non-streaming chat completions through owned HTTP and supports
`bearer` or `none` authentication. Anonymous requests send no placeholder
Authorization header. Existing `openai` and `openrouter` adapters retain their
pinned SDK behavior, including Responses, embeddings, Decisions and OpenRouter
native X accounting. They require bearer authentication. `operations` limits
alias selection; `openai_chat` supports only `[chat]`.

`kind` remains the legacy spelling of `adapter` and is used in resolved snapshots
and inspection responses. Legacy `kind: openai` and `kind: openrouter` entries
receive defaults from the corresponding bundled file.

Base URLs require HTTPS on port 443, a DNS hostname and a simple API path.
Credentials in URLs, IP literals, controls, queries, fragments, path traversal and
redirects are rejected. Chat posts to `<base_url>/chat/completions`. Callers and
model names cannot override destinations. Direct connections and the egress proxy
reject private or mixed public/private DNS results.

Free-only alias pricing requires an upstream model matching `free_models` or a
declared `free_model_suffixes` entry on its provider. These are reviewed operator
declarations, not live pricing discovery or a guarantee of future provider terms.
Free-only routes cannot enable native X, tools with fees or gateway fallback
routes. Paid aliases retain their budget requirements. Unknown usage and billing
measurements remain unknown.

## Model alias schema

`models/gpt-example.yaml` is a complete existing AliasConfig:

```yaml
provider_ref: router
upstream_model: openai/example-model:free
operation: chat
capabilities: [plain_text]
limits:
  max_input_bytes: 4096
  max_input_tokens: 8192
  max_output_tokens: 256
  attempt_timeout_seconds: 60
  request_timeout_seconds: 90
  max_concurrency: 1
pricing:
  revision: reviewed-example
  bound_source: https://provider.example/reviewed-pricing
  free_only: true
```

These model names are illustrative. The callsite uses
`client.chat("gpt-example", chat_input)`. The alias key is the public model name;
the upstream identifier is gateway configuration.

## Snapshots, reloads and egress

Definitions are resolved and validated before activation. Snapshots contain
complete definitions, not file references. Their digests cover resolved values;
changing a file changes the next snapshot. Existing request IDs retain their
original configuration and receipts. An invalid file leaves active configuration
unchanged. The generated [ProjectConfig schema](api/ProjectConfig.schema.json)
describes resolved snapshots; references are a loader input feature.

Start the egress proxy with `--project-config /config/project.yaml` and mount the
same configuration directory read-only in both services. It loads enabled provider
hostnames without resolving credentials and receives no secret/token mounts.
Recreate the proxy when destinations change, then reload the gateway. Stale
permissions fail closed. Legacy proxy invocations without `--project-config`
permit only the two bundled destinations; the Compose templates pass explicit
project configuration.

The runnable [Kilo example](../examples/free-chat/README.md) supplies a local
provider and model alias, consumed through a released Docker image.
