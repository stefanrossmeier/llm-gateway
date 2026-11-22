# Third-party dependencies and attribution

Authored gateway code and documentation use [MIT](LICENSE), copyright 2026 Stefan
Rossmeier. Dependencies are separate works and retain their licenses, notices and
obligations. This file does not relicense them.

The runtime uses installed LiteLLM, including a reviewed version-specific Decisions
seam, without vendoring its source. The lean client uses HTTPX/Pydantic. The
service adds LiteLLM, FastAPI, Uvicorn, PyYAML, jsonschema and locked transitive
dependencies. Python/uv images and base OS components have their own licenses.

Exact versions are in [uv.lock](uv.lock) and the
[CycloneDX dependency SBOM](docs/evidence/dependency-sbom.cdx.json). That SBOM covers
the union of client/service/development dependencies; it is not the complete OS
SBOM of an image. Preserve upstream distribution notices when redistributing and
generate a complete image SBOM at release.

The README layout is inspired by Safeplane; its wordmark/text are original.
Research references do not grant permission to republish private sources,
fixtures or infrastructure. See [publication review](docs/PUBLICATION_REVIEW.md).
