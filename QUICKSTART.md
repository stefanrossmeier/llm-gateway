# Quickstart

Run a fake inference without credentials or provider access. Install uv and use
Python 3.12 for the service.

```sh
git clone https://github.com/stefanrossmeier/llm-gateway.git
cd llm-gateway
git checkout v0.1.0a4
uv sync --frozen --extra service --no-editable
demo=$(mktemp -d /tmp/gateway-demo.XXXXXX)
.venv/bin/python scripts/create_fake_project.py --directory "$demo"
.venv/bin/llm-gateway config validate --project-config "$demo/project.yaml"
.venv/bin/llm-gateway serve --project-config "$demo/project.yaml"   --state-dir "$demo/state" --unix-socket "$demo/gateway.sock"
```

The server stays in the foreground. In a second terminal, from the checkout,
replace `/tmp/gateway-demo.REPLACE` with the printed directory name:

```sh
.venv/bin/python - <<'PYCODE'
from llm_gateway.client import GatewayClient
from llm_gateway.contracts import ChatInput, Message

with GatewayClient("http://localhost", unix_socket="/tmp/gateway-demo.REPLACE/gateway.sock") as client:
    result = client.chat("example-chat", ChatInput(messages=[Message(role="user", content="Hello")]),
                         request_id="demo:one")
    print(result.content)
    print(result.receipts[0].evidence_status)  # simulated
PYCODE
```

Stop with Ctrl-C. The fixture contains one response. Reusing `demo:one` recovers
its existing receipt; default metadata-only retention does not replay content.
Create a new temporary demo directory for a new complete run.

For real use, install a reviewed release, provision a private token, and supply
project-owned live configuration. Persist the logical run/request IDs before
calling inference; retain results if automatic lost-response recovery needs
content. Read [configuration](docs/CONFIGURATION.md), [tokens](docs/TOKENS.md),
[accounting](docs/ACCOUNTING.md) and [X support](docs/X_SEARCH.md) first.

For real inference using the released Docker image and anonymous free Kilo
access, see [examples/free-chat](examples/free-chat/README.md).
