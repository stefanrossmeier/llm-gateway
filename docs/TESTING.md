# Testing

Default pytest runs deny inference networking and remove provider credentials.
Use synthetic inputs and actual mocked SDK transport adapters to test money,
identity, state recovery, validation and isolation boundaries.

```sh
uv sync --frozen --extra service --no-editable --reinstall-package llm-gateway
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/pytest -q --cov=llm_gateway --cov-branch --cov-report=json:coverage.json
.venv/bin/python scripts/check_coverage.py coverage.json
.venv/bin/python scripts/export_schemas.py
```

Every authored module must meet the branch gate without exclusions. Regenerated
schemas must match committed contracts. Container tests verify fake inference,
shutdown, non-root/read-only restrictions and owned process/network behavior.
Native probes exercise eight mocked adapter calls without provider inference.

Live checks are separate. Free routing can still reject unsupported capabilities;
record that outcome accurately. Paid tests need a concrete reviewed plan and
explicit authorization. X's allowance cannot be described as a guaranteed paid
test ceiling. Never weaken parameter validation or silently fall back to a paid
model just to turn a failed free test green. See [live plans](LIVE_TEST_PLANS.md).
