# Installation

Use an immutable release tag, verified wheel or image identity. Do not deploy
from a moving branch or depend on another project's source checkout.

```sh
git clone --branch v0.1.0a3 https://github.com/stefanrossmeier/llm-gateway.git
cd llm-gateway
uv sync --frozen --extra service --no-editable
.venv/bin/llm-gateway --version
```

The service requires Python 3.12/POSIX. The lean client supports Python 3.11 and
3.12 and does not import LiteLLM, OpenAI or FastAPI. Install the release wheel
without extras for a client; install `[service]` with the matching frozen graph
for a service. A wheel alone does not pin transitive dependencies.

Release assets contain hashes, saved Linux images and their evidence. Verify the
manifest and SHA-256 checksums before use. Run `scripts/check_release_assets.py`
with the release manifest and a new absolute destination to verify/download
assets. Existing files are never overwritten by that tool.

```sh
docker build -t llm-gateway:candidate .
python3 scripts/check_container.py --image llm-gateway:candidate
```

[Compose templates](../deploy/compose.yaml) require an explicit immutable image
and private project paths. Provider keys, bearer tokens and accounting state are
runtime mounts, never image inputs. Read [operations](OPERATIONS.md).
