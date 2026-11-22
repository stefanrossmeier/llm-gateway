"""Authenticated HTTP surfaces over one engine. No endpoint invokes an SDK."""

import asyncio
import json
import re
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from llm_gateway import __version__
from llm_gateway.config import ConfigError
from llm_gateway.config.models import CAPABILITY_REVISION
from llm_gateway.config.secrets import resolve_credential
from llm_gateway.contracts import InferenceRequest, SafeError
from llm_gateway.storage.ledger import LedgerError

from .auth import Authenticator, require
from .compatibility import dashboard, only, openai_request, openai_response
from .engine import EngineError
from .wire import AllowanceReview, DashboardChat, Reconcile, SafeplaneChat, SafeplaneDecisions

MAX_BODY = 16 * 1024 * 1024
DEFAULT_BODY = 1024 * 1024
MAX_RESULT = 16 * 1024 * 1024


def validate_body_limit(value):
    if type(value) is not int or not 1 <= value <= MAX_BODY:
        raise EngineError("http_body_limit_invalid")


def create_app(
    engine,
    *,
    project_configs=None,
    owner_socket=None,
    ipc_project=None,
    max_body_bytes=DEFAULT_BODY,
):
    validate_body_limit(max_body_bytes)
    app = FastAPI(title="LLM Gateway", version=__version__, docs_url=None, redoc_url=None)
    auth = Authenticator(engine.registry, owner_socket=owner_socket, ipc_project=ipc_project)
    project_configs = project_configs or {}
    submissions, submission_lock = 0, threading.Lock()

    @app.middleware("http")
    async def bounded_body(request, call_next):
        request.state.trace_id = uuid.uuid4().hex
        request.state.received_at = datetime.now(UTC)
        if request.headers.get("content-encoding", "identity") not in {"identity", ""}:
            return JSONResponse(
                {"error": "request_compression_unsupported"},
                status_code=415,
                headers={"X-Gateway-Trace-Id": request.state.trace_id},
            )
        if request.url.path != "/health":
            try:
                authenticate(request)
            except (EngineError, LedgerError, ConfigError) as error:
                return await safe_error(request, error)
            except (sqlite3.Error, OSError) as error:
                return await unavailable(request, error)
            size, chunks = 0, []
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_body_bytes:
                    return JSONResponse(
                        {"error": "request_body_too_large"},
                        status_code=413,
                        headers={"X-Gateway-Trace-Id": request.state.trace_id},
                    )
                chunks.append(chunk)
            request._body = b"".join(chunks)
        else:
            request._body = b""
        response = await call_next(request)
        if "X-Gateway-Trace-Id" not in response.headers:
            response.headers["X-Gateway-Trace-Id"] = request.state.trace_id
        return response

    def authenticate(request, permission=None):
        authority = getattr(request.state, "authority", None)
        if authority is None:
            authority = auth.authenticate(
                request.headers.get("authorization"), client=request.scope.get("client")
            )
        elif authority.config_digest != engine.registry.get(authority.project).digest:
            raise EngineError("configuration_changed_before_admission")
        if permission:
            require(authority, permission)
        request.state.authority = authority
        return authority

    async def body(request):
        try:
            data = json.loads(
                await request.body(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
            )
        except (ValueError, UnicodeError):
            raise EngineError("invalid_json") from None
        if not isinstance(data, dict):
            raise EngineError("object_body_required")
        return data

    def identity(request, data):
        header, field = request.headers.get("idempotency-key"), data.get("request_id")
        if header and field and header != field:
            raise EngineError("idempotency_conflict")
        value = header or field
        if not value:
            raise HTTPException(428, "idempotency_required")
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
            raise EngineError("idempotency_invalid")
        request.state.request_id = value
        return value

    def headers(result):
        data = {
            "X-Gateway-Trace-Id": result.trace_id,
            "X-Gateway-Request-Id": result.request_id,
            "X-Gateway-Config-Digest": result.config_digest,
            "X-Gateway-Output-Status": result.output_status,
        }
        if result.receipts:
            data["X-Gateway-Receipt-Id"] = result.receipts[-1].receipt_id
        return data

    def result_response(result, projection=None):
        if result.execution_status == "in_progress":
            code = 202
        elif result.provider_metadata.get("result_available") is False:
            code = 410
        elif result.execution_status in {"timed_out", "cancelled"}:
            code = 504
        elif result.output_status != "valid":
            code = {"provider_auth": 503, "provider_quota": 429}.get(
                result.error.code if result.error else "", 502
            )
        else:
            code = 200
        response = JSONResponse(
            projection
            if projection is not None and code == 200
            else result.model_dump(mode="json"),
            status_code=code,
            headers=headers(result),
        )
        if len(response.body) > MAX_RESULT:
            metadata = headers(result)
            metadata["X-Gateway-Output-Status"] = "unavailable"
            error = SafeError(
                code="result_output_too_large",
                message="Gateway response exceeds the output limit; inspect its receipt.",
                advice="query_receipt",
                trace_id=result.trace_id,
                receipt_id=result.receipts[-1].receipt_id if result.receipts else None,
            )
            return JSONResponse(
                {
                    "error": error.model_dump(mode="json"),
                    "receipts": [receipt.model_dump(mode="json") for receipt in result.receipts],
                },
                status_code=502,
                headers=metadata,
            )
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, error):
        return JSONResponse(
            {
                "error": SafeError(
                    code="idempotency_required" if error.status_code == 428 else "http_error",
                    message="Gateway HTTP request rejected.",
                    advice="correct_request",
                    trace_id=request.state.trace_id,
                ).model_dump(mode="json")
            },
            status_code=error.status_code,
            headers={"X-Gateway-Trace-Id": request.state.trace_id},
        )

    @app.exception_handler(ValidationError)
    @app.exception_handler(KeyError)
    async def validation_error(request, error):
        return JSONResponse(
            {
                "error": SafeError(
                    code="request_schema_invalid",
                    message="Request does not match the supported schema.",
                    advice="correct_request",
                    trace_id=request.state.trace_id,
                ).model_dump(mode="json")
            },
            status_code=422,
            headers={"X-Gateway-Trace-Id": request.state.trace_id},
        )

    @app.exception_handler(EngineError)
    @app.exception_handler(LedgerError)
    @app.exception_handler(ConfigError)
    async def safe_error(request, error):
        raw = getattr(error, "code", None) or str(error)
        code = raw if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw) else "request_rejected"
        status = 422
        if code.startswith("authentication_"):
            status = 401
        elif "forbidden" in code or "binding" in code:
            status = 403
        elif (
            "conflict" in code
            or "hold" in code
            or "quota_consumed" in code
            or code in {"evidence_identity_mismatch", "configuration_changed_before_admission"}
        ):
            status = 409
        elif "unknown" in code:
            status = 404
        elif "budget_exceeded" in code or code == "queue_full":
            status = 429
        elif "unavailable" in code or "fenced" in code or code == "service_shutting_down":
            status = 503
        elif "deadline" in code:
            status = 504
        metadata = {"X-Gateway-Trace-Id": request.state.trace_id}
        receipts = []
        authority = getattr(request.state, "authority", None)
        request_id = getattr(request.state, "request_id", None)
        if authority and request_id:
            try:
                record = engine.ledger.request(authority.project, authority.principal, request_id)
                receipts = engine.receipts(authority, request_id)
                metadata.update(
                    {
                        "X-Gateway-Trace-Id": record["trace_id"],
                        "X-Gateway-Request-Id": request_id,
                        "X-Gateway-Config-Digest": record["config_digest"],
                    }
                )
                if receipts:
                    metadata["X-Gateway-Receipt-Id"] = receipts[-1].receipt_id
            except Exception:
                pass
        safe = SafeError(
            code=code,
            message="Gateway rejected the operation; inspect its receipt if admitted.",
            advice="query_receipt" if "X-Gateway-Request-Id" in metadata else "correct_request",
            trace_id=metadata["X-Gateway-Trace-Id"],
            receipt_id=metadata.get("X-Gateway-Receipt-Id"),
        )
        return JSONResponse(
            {
                "error": safe.model_dump(mode="json"),
                "receipts": [r.model_dump(mode="json") for r in receipts],
            },
            status_code=status,
            headers=metadata,
        )

    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(OSError)
    async def unavailable(request, error):
        engine.fenced = True
        return await safe_error(request, EngineError("ledger_unavailable"))

    async def invoke(request, authority, canonical):
        nonlocal submissions
        with submission_lock:
            if submissions >= engine.max_queue + engine.max_concurrency:
                raise EngineError("queue_full")
            submissions += 1
        signal = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                engine.run,
                authority,
                canonical,
                cancel_signal=signal,
                received_at=request.state.received_at,
                expected_config_digest=request.headers.get("x-gateway-expected-config-digest"),
            )
        )
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.1)
                if not task.done() and await request.is_disconnected():
                    signal.set()
                    try:
                        engine.cancel(authority, canonical.request_id)
                    except LedgerError:
                        pass
            return await task
        except asyncio.CancelledError:
            signal.set()
            try:
                engine.cancel(authority, canonical.request_id)
            except LedgerError:
                pass
            raise
        finally:
            with submission_lock:
                submissions -= 1

    @app.get("/health")
    def health():
        return {"status": "alive"}

    @app.get("/ready")
    async def ready(request: Request):
        authority = authenticate(request)
        if not authority.grants.permissions:
            raise EngineError("permission_forbidden")
        snapshot = engine.registry.get(authority.project)
        if snapshot.capability_revision != CAPABILITY_REVISION:
            raise EngineError("configuration_capability_unavailable_reload_required")
        if engine.fenced or engine.closing:
            raise EngineError("ledger_unavailable" if engine.fenced else "service_shutting_down")
        with engine.store.connection() as connection:
            if connection.execute(
                "SELECT 1 FROM runtime_flags WHERE name IN ('admission_fenced',?)",
                ("project_fenced:" + authority.project,),
            ).fetchone():
                raise EngineError("admission_fenced")
        if snapshot.config.mode == "live":
            used = {
                profile.provider_ref
                for name, profile in snapshot.config.aliases.items()
                if name in authority.grants.aliases
            }
            used.update(
                route.provider_ref
                for name, profile in snapshot.config.aliases.items()
                if name in authority.grants.aliases
                for route in profile.attempt_policy.fallback_routes
            )
            for name, provider in snapshot.config.providers.items():
                if name in used and provider.enabled and provider.credential is not None:
                    try:
                        resolve_credential(provider.credential)
                    except ValueError:
                        raise EngineError("provider_credential_unavailable") from None
        return {
            "status": "ready",
            "version": __version__,
            "supports_config_precondition": True,
            "principal_id": authority.principal,
            "project_id": authority.project,
            "config_digest": snapshot.digest,
        }

    @app.get("/v1/aliases")
    async def aliases(request: Request):
        authority = authenticate(request, "infer")
        snapshot = engine.registry.get(authority.project)
        return {
            "config_digest": snapshot.digest,
            "aliases": [
                {
                    "alias": name,
                    "operation": profile.operation,
                    "provider_ref": profile.provider_ref,
                    "provider_kind": snapshot.config.providers[profile.provider_ref].kind,
                    "upstream_model": profile.upstream_model,
                    "capabilities": profile.capabilities,
                    "limits": profile.limits.model_dump(mode="json"),
                }
                for name, profile in snapshot.config.aliases.items()
                if name in authority.grants.aliases
                and profile.operation in authority.grants.operations
            ],
        }

    @app.post("/v1/inferences")
    async def infer(request: Request):
        authority = authenticate(request, "infer")
        data = await body(request)
        data["request_id"] = identity(request, data)
        canonical = InferenceRequest.model_validate(data)
        return result_response(await invoke(request, authority, canonical))

    @app.get("/v1/requests/{request_id}")
    async def lookup(request_id: str, request: Request):
        authority = authenticate(request, "receipts:own")
        result = engine.lookup(authority, request_id)
        format = request.query_params.get("format")
        if format not in {None, "dashboard"}:
            raise EngineError("unsupported_result_format")
        return result_response(
            result, dashboard(result, latency_ms=0) if format == "dashboard" else None
        )

    @app.post("/v1/requests/{request_id}/cancel")
    async def cancel(request_id: str, request: Request):
        authority = authenticate(request, "receipts:own")
        engine.cancel(authority, request_id)
        return JSONResponse({"status": "cancellation_requested"}, status_code=202)

    @app.get("/v1/receipts/{receipt_id}")
    async def receipt(receipt_id: str, request: Request):
        authority = authenticate(request, "receipts:own")
        with engine.store.connection() as connection:
            row = connection.execute(
                "SELECT request_id FROM attempts WHERE attempt_id=? AND project_id=? "
                "AND principal_id=?",
                (receipt_id, authority.project, authority.principal),
            ).fetchone()
        if not row:
            raise LedgerError("receipt_unknown")
        result = next(r for r in engine.receipts(authority, row[0]) if r.receipt_id == receipt_id)
        return JSONResponse(
            result.model_dump(mode="json"),
            headers={
                "X-Gateway-Trace-Id": engine.ledger.request(
                    authority.project, authority.principal, row[0]
                )["trace_id"],
                "X-Gateway-Receipt-Id": receipt_id,
                "X-Gateway-Config-Digest": result.config_digest,
            },
        )

    @app.get("/v1/usage")
    async def report(request: Request):
        authority = authenticate(request)
        require(
            authority,
            "usage:project" if "usage:project" in authority.grants.permissions else "receipts:own",
        )
        filters = dict(request.query_params)
        group = filters.pop("group_by", None)
        for name in ("run_id", "phase"):
            binding = getattr(authority.grants, name)
            if binding is not None:
                if name in filters and filters[name] != binding:
                    raise EngineError("report_binding_forbidden")
                filters[name] = binding
        return engine.ledger.report(
            authority.project,
            principal=None
            if "usage:project" in authority.grants.permissions
            else authority.principal,
            filters=filters,
            group_by=group,
            timezone=engine.registry.get(authority.project).config.timezone,
        )

    @app.post("/v1/admin/receipts/{receipt_id}/reconcile")
    async def reconcile(receipt_id: str, request: Request):
        authority = authenticate(request, "admin:reconcile")
        evidence = Reconcile.model_validate(await body(request))
        with engine.store.connection() as connection:
            original = connection.execute(
                "SELECT a.route_json,r.config_digest FROM attempts a "
                "JOIN requests r USING(project_id,principal_id,request_id) WHERE a.project_id=? "
                "AND a.attempt_id=?",
                (authority.project, receipt_id),
            ).fetchone()
        if original is None:
            raise LedgerError("receipt_unknown")
        if (
            original[1] != evidence.config_digest
            or json.loads(original[0])["provider_ref"] != evidence.provider_ref
        ):
            raise LedgerError("evidence_identity_mismatch")
        changed = engine.ledger.reconcile(
            authority.project,
            evidence.evidence_id,
            attempt_id=receipt_id,
            request_id=evidence.request_id,
            generation_id=evidence.generation_id,
            measured=evidence.measured_usd,
            authority=json.dumps(
                {
                    "actor": authority.principal,
                    "source": evidence.authority,
                    "verification": evidence.verification,
                    "digest": evidence.evidence_digest,
                    "provider_ref": evidence.provider_ref,
                    "config_digest": evidence.config_digest,
                },
                sort_keys=True,
            ),
            observed_at=evidence.observed_at,
        )
        return {"changed": changed}

    @app.post("/v1/admin/receipts/{receipt_id}/acknowledge-allowance")
    async def acknowledge_allowance(receipt_id: str, request: Request):
        authority = authenticate(request, "admin:reconcile")
        review = AllowanceReview.model_validate(await body(request))
        snapshot = engine.registry.get(authority.project)
        if review.config_digest != snapshot.digest:
            raise EngineError("expected_configuration_conflict")
        with engine.store.connection() as connection:
            original = connection.execute(
                "SELECT r.alias FROM attempts a JOIN requests r "
                "USING(project_id,principal_id,request_id) WHERE a.project_id=? AND a.attempt_id=?",
                (authority.project, receipt_id),
            ).fetchone()
        if original is None:
            raise LedgerError("receipt_unknown")
        profile = snapshot.config.aliases.get(original[0])
        if (
            profile is None
            or profile.operation != "x_search"
            or profile.pricing is None
            or not profile.pricing.accept_unbounded_x_charges
            or profile.pricing.x_request_allowance_usd is None
        ):
            raise EngineError("allowance_review_invalid")
        changed = engine.ledger.acknowledge_allowance(
            authority.project,
            receipt_id,
            allowance=profile.pricing.x_request_allowance_usd,
            evidence_id=review.evidence_id,
            actor=authority.principal,
            reason=review.reason,
        )
        return {"changed": changed}

    @app.post("/v1/admin/reload")
    async def reload(request: Request):
        authority = authenticate(request, "admin:reload")
        only(await body(request), set())
        path = project_configs.get(authority.project)
        if path is None:
            raise EngineError("reload_path_unavailable")
        snapshot = engine.registry.reload(path, authority.project)
        return {"config_digest": snapshot.digest}

    @app.post("/v1/chat")
    async def dashboard_chat(request: Request):
        start = time.monotonic()
        authority = authenticate(request, "infer")
        data = await body(request)
        identifier = identity(request, data)
        wire = DashboardChat.model_validate(data)
        config = engine.registry.get(authority.project).config
        if wire.alias not in config.compatibility.dashboard_profiles.get(wire.profile, []):
            raise EngineError("profile_alias_forbidden")
        canonical = InferenceRequest.model_validate(
            {
                "request_id": identifier,
                "alias": wire.alias,
                "operation": "chat",
                "input": {
                    "kind": "chat",
                    "messages": [m.model_dump(mode="json") for m in wire.messages],
                    "response_format": "json_schema",
                    "response_schema": wire.response_schema,
                    "schema_name": wire.response_schema_name,
                },
                "context": {
                    "run_id": wire.run_id or authority.grants.run_id,
                    "purpose": wire.purpose,
                    "profile": wire.profile,
                    "phase": authority.grants.phase or "task",
                },
            }
        )
        result = await invoke(request, authority, canonical)
        return result_response(
            result, dashboard(result, latency_ms=int((time.monotonic() - start) * 1000))
        )

    @app.post("/chat")
    async def safeplane_chat(request: Request):
        authority = authenticate(request, "infer")
        data = await body(request)
        identifier = identity(request, data)
        wire = SafeplaneChat.model_validate(data)
        config = engine.registry.get(authority.project).config
        alias = config.compatibility.safeplane_profiles.get(wire.workflow_id, {}).get(
            wire.model_profile
        )
        if not alias:
            raise EngineError("profile_alias_forbidden")
        canonical = InferenceRequest.model_validate(
            {
                "request_id": identifier,
                "alias": alias,
                "operation": "chat",
                "input": {
                    "kind": "chat",
                    "messages": [m.model_dump(mode="json") for m in wire.messages],
                },
                "context": {
                    "run_id": wire.run_id or authority.grants.run_id,
                    "workflow_id": wire.workflow_id,
                    "session_id": wire.session_id,
                    "stage_id": wire.stage_id,
                    "turn": wire.turn,
                    "profile": wire.model_profile,
                    "phase": authority.grants.phase or "task",
                },
                "limits": {"timeout_seconds": wire.timeout_seconds},
            }
        )
        result = await invoke(request, authority, canonical)
        projection = {
            "status": "completed",
            "message": {
                "role": "assistant",
                "content": result.content
                if isinstance(result.content, str)
                else json.dumps(result.content),
            },
            "model": {
                "mode": config.mode,
                "configured_model": result.model.configured_model,
                "actual_model": result.model.actual_model,
                "actual_provider": result.model.actual_provider,
                "generation_id": result.model.generation_id,
                "workflow_id": wire.workflow_id,
                "model_profile": wire.model_profile,
            },
            "usage": dashboard(result, latency_ms=0)["usage"],
            "finish_reason": result.finish_reason,
        }
        return result_response(result, projection)

    @app.post("/decisions")
    async def decisions(request: Request):
        authority = authenticate(request, "infer")
        data = await body(request)
        identifier = identity(request, data)
        wire = SafeplaneDecisions.model_validate(data)
        canonical = InferenceRequest.model_validate(
            {
                "request_id": identifier,
                "alias": wire.model,
                "operation": "decisions",
                "input": {"kind": "decisions", "state": wire.state, "questions": wire.questions},
                "context": {
                    "run_id": wire.run_id or authority.grants.run_id,
                    "phase": authority.grants.phase or "task",
                },
                "limits": {"timeout_seconds": wire.timeout_seconds},
            }
        )
        result = await invoke(request, authority, canonical)
        return result_response(
            result,
            {
                "id": result.model.generation_id,
                "model": result.model.actual_model or result.model.upstream_model,
                "provider": result.model.actual_provider,
                "answers": result.content.get("answers", {})
                if isinstance(result.content, dict)
                else {},
                "usage": dashboard(result, latency_ms=0)["usage"],
            },
        )

    @app.post("/v1/x-search")
    async def x_search(request: Request):
        start = time.monotonic()
        authority = authenticate(request, "infer")
        data = await body(request)
        identifier = identity(request, data)
        only(
            data,
            {
                "request_id",
                "alias",
                "purpose",
                "profile",
                "run_id",
                "input",
                "allowed_x_handles",
                "from_date",
                "to_date",
                "response_schema_name",
                "response_schema",
            },
        )
        config = engine.registry.get(authority.project).config
        if data["alias"] not in config.compatibility.dashboard_profiles.get(data["profile"], []):
            raise EngineError("profile_alias_forbidden")
        canonical = InferenceRequest.model_validate(
            {
                "request_id": identifier,
                "alias": data["alias"],
                "operation": "x_search",
                "input": {
                    "kind": "x_search",
                    **{
                        k: data[k]
                        for k in (
                            "input",
                            "allowed_x_handles",
                            "from_date",
                            "to_date",
                            "response_schema",
                        )
                    },
                    "schema_name": data["response_schema_name"],
                },
                "context": {
                    "run_id": data.get("run_id") or authority.grants.run_id,
                    "purpose": data["purpose"],
                    "profile": data["profile"],
                    "phase": authority.grants.phase or "task",
                },
            }
        )
        result = await invoke(request, authority, canonical)
        return result_response(
            result, dashboard(result, latency_ms=int((time.monotonic() - start) * 1000))
        )

    for path, operation in (
        ("/v1/responses", "responses"),
        ("/v1/chat/completions", "chat"),
        ("/v1/embeddings", "embeddings"),
    ):

        def endpoint(operation):
            async def handle(request: Request):
                authority = authenticate(request, "infer")
                data = await body(request)
                canonical = openai_request(operation, data, identity(request, data))
                canonical = canonical.model_copy(
                    update={
                        "context": canonical.context.model_copy(
                            update={
                                "run_id": authority.grants.run_id,
                                "phase": authority.grants.phase or "task",
                            }
                        )
                    }
                )
                result = await invoke(request, authority, canonical)
                return result_response(result, openai_response(result, operation))

            return handle

        app.add_api_route(path, endpoint(operation), methods=["POST"])
    return app
