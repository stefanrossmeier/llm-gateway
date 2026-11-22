"""Generate nonsecret contracts and annotated OpenAPI without a real project."""

import json
from pathlib import Path
from types import SimpleNamespace

from llm_gateway.config.models import ProjectConfig
from llm_gateway.contracts import AttemptReceipt, InferenceRequest, InferenceResult, SafeError
from llm_gateway.contracts.reports import AliasReport, Readiness, UsageReport
from llm_gateway.service.api import create_app
from llm_gateway.service.wire import (
    AllowanceReview,
    DashboardChat,
    Reconcile,
    SafeplaneChat,
    SafeplaneDecisions,
)


def generated():
    models = [
        AllowanceReview,
        ProjectConfig,
        InferenceRequest,
        InferenceResult,
        AttemptReceipt,
        SafeError,
        UsageReport,
        AliasReport,
        Readiness,
        DashboardChat,
        Reconcile,
        SafeplaneChat,
        SafeplaneDecisions,
    ]
    schema = create_app(SimpleNamespace(registry=None)).openapi()
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    files = {}
    for model in models:
        files[model.__name__ + ".schema.json"] = model.model_json_schema()
        reference = model.model_json_schema(ref_template="#/components/schemas/{model}")
        components.update(reference.pop("$defs", {}))
        components[model.__name__] = reference
    schema["components"]["securitySchemes"] = {
        "GatewayBearer": {"type": "http", "scheme": "bearer"}
    }
    mappings = {
        "/v1/inferences": ("InferenceRequest", "InferenceResult"),
        "/v1/chat": ("DashboardChat", None),
        "/chat": ("SafeplaneChat", None),
        "/decisions": ("SafeplaneDecisions", None),
        "/v1/admin/receipts/{receipt_id}/reconcile": ("Reconcile", None),
        "/v1/admin/receipts/{receipt_id}/acknowledge-allowance": ("AllowanceReview", None),
    }
    gets = {
        "/v1/requests/{request_id}": "InferenceResult",
        "/v1/receipts/{receipt_id}": "AttemptReceipt",
        "/v1/usage": "UsageReport",
        "/v1/aliases": "AliasReport",
        "/ready": "Readiness",
    }
    for path, methods in schema["paths"].items():
        for operation in methods.values():
            if path != "/health":
                operation["security"] = [{"GatewayBearer": []}]
            if path in {
                "/v1/inferences",
                "/v1/chat",
                "/chat",
                "/decisions",
                "/v1/x-search",
                "/v1/responses",
                "/v1/chat/completions",
                "/v1/embeddings",
            }:
                operation.setdefault("parameters", []).append(
                    {
                        "name": "X-Gateway-Expected-Config-Digest",
                        "in": "header",
                        "required": False,
                        "schema": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                        "description": (
                            "Require this immutable config for new admissions (409 on mismatch). "
                            "Existing IDs replay their original snapshot."
                        ),
                    }
                )
    for path, (request, response) in mappings.items():
        operation = schema["paths"][path]["post"]
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/" + request}}
            },
        }
        if response:
            operation["responses"]["200"] = {
                "description": "Canonical inference result",
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/" + response}}
                },
            }
    for path, response in gets.items():
        schema["paths"][path]["get"]["responses"]["200"] = {
            "description": "Scoped result",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/" + response}}
            },
        }
    schema["info"]["description"] = (
        "Gateway bearer authentication or verified private owner Unix IPC. "
        "Compatibility bodies outside explicit schemas use the restricted subset in docs/API.md. "
        "Inference/error receipts remain authoritative on non-200 statuses."
    )
    files["openapi.json"] = schema
    return files


def main():
    root = Path(__file__).resolve().parents[1] / "docs" / "api"
    root.mkdir(exist_ok=True)
    for filename, data in generated().items():
        (root / filename).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
