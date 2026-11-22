```text
██╗      ██╗      ███╗   ███╗     ██████╗   █████╗  ████████╗ ███████╗ ██╗    ██╗  █████╗  ██╗   ██╗
██║      ██║      ████╗ ████║    ██╔════╝  ██╔══██╗ ╚══██╔══╝ ██╔════╝ ██║    ██║ ██╔══██╗ ╚██╗ ██╔╝
██║      ██║      ██╔████╔██║    ██║  ███╗ ███████║    ██║    █████╗   ██║ █╗ ██║ ███████║  ╚████╔╝
██║      ██║      ██║╚██╔╝██║    ██║   ██║ ██╔══██║    ██║    ██╔══╝   ██║███╗██║ ██╔══██║   ╚██╔╝
███████╗ ███████╗ ██║ ╚═╝ ██║    ╚██████╔╝ ██║  ██║    ██║    ███████╗ ╚███╔███╔╝ ██║  ██║    ██║
╚══════╝ ╚══════╝ ╚═╝     ╚═╝     ╚═════╝  ╚═╝  ╚═╝    ╚═╝    ╚══════╝  ╚══╝╚══╝  ╚═╝  ╚═╝    ╚═╝

                   Shared inference, project-owned policy, accountable spending.
```

LLM Gateway is a Python service and lean client for authenticated OpenAI and
OpenRouter access. Each project owns its aliases, models, credentials and budgets.
The gateway owns transport, validation, durable accounting and request recovery.

**Version 0.1.0a3 — alpha.** The client supports Python 3.11–3.12; the service
requires Python 3.12 on POSIX. This release includes chat, strict JSON, stateless
Responses and function calls, embeddings, Decisions, and scoped native X search
through OpenRouter. Provider/model capabilities still require validation.

Start with [QUICKSTART.md](QUICKSTART.md), which needs no keys or inference.
See the [documentation index](docs/README.md) for installation and operations.

## Accounting you can inspect

Every logical invocation has a stable ID and durable attempt receipts. Decimal
USD accounting distinguishes measured spending, estimates, protected debits and
unknown exposure. `GET /v1/usage` supports date windows and grouping by alias,
provider, model, run, local day and month. Never add estimates to measured costs
or sum budget buckets: the same attempt can participate in several policies.

Native X bills fetched posts and profiles plus cumulative agentic tokens. Its
explicit risk allowance is **not a guaranteed provider spending cap**. Live X
requires operator acceptance of that risk. Missing measurements remain unknown;
uncertain calls retain exposure, and overruns stop subsequent admission pending
review. See [X support](docs/X_SEARCH.md) and [cost reporting](docs/ACCOUNTING.md).

## Boundaries

- Separate project configuration, tokens and private SQLite state.
- Bearer authentication over HTTP, or verified private Unix IPC.
- Selected provider credentials stay in owned provider workers; schema validation is key-free.
- Explicit attempts, timeouts, output limits and recovery; no hidden SDK retries.
- Frozen dependencies, non-root/read-only container checks and exact-artifact advisory review.

Container assessments are scoped evidence, not a claim that every bundled
component is patched. Use the documented restricted profile. This alpha does
not provide an LTS or universal production-conformance promise.

## Development

```sh
uv sync --frozen --extra service --no-editable
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/pytest -q --cov=llm_gateway --cov-branch --cov-report=json:coverage.json
.venv/bin/python scripts/check_coverage.py coverage.json
```

Default tests deny inference networking. Live tests require explicit enablement
and their own reviewed scope. See [testing](docs/TESTING.md),
[contributing](CONTRIBUTING.md), [security](SECURITY.md) and the [MIT license](LICENSE).
