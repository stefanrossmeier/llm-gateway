"""One canonical inference path. HTTP adapters never own transport or money."""

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from llm_gateway.config.models import CAPABILITY_REVISION, PrincipalConfig
from llm_gateway.config.registry import ConfigRegistry
from llm_gateway.contracts import (
    AttemptReceipt,
    InferenceResult,
    ModelIdentity,
    SafeError,
    TokenUsage,
)
from llm_gateway.providers.pricing import estimate_usd, reserve_usd
from llm_gateway.providers.process import OwnedProviderProcess
from llm_gateway.providers.runtime import ProviderEvidence, ProviderFailure
from llm_gateway.storage.buckets import bind_bucket
from llm_gateway.storage.ledger import Ledger, LedgerError, exact_money
from llm_gateway.storage.periods import period_window
from llm_gateway.storage.results import Results

from .output import Output
from .validation import ValidationProcess
from .x_scope import subset_identity


@dataclass(frozen=True)
class Authority:
    project: str
    principal: str
    grants: PrincipalConfig
    config_digest: str | None = None
    owner_ipc: bool = False


class EngineError(ValueError):
    pass


class Engine:
    def __init__(self, store, *, provider=None, max_queue=256, max_concurrency=8):
        if (
            type(max_queue) is not int
            or type(max_concurrency) is not int
            or not 1 <= max_queue <= 1024
            or not 1 <= max_concurrency <= 64
        ):
            raise EngineError("engine_capacity_invalid")
        self.store = store
        self.registry, self.ledger, self.results = (
            ConfigRegistry(store),
            Ledger(store),
            Results(store),
        )
        self.provider = provider or OwnedProviderProcess(store.root)
        self.validation = ValidationProcess(store.root)
        self.condition = threading.Condition()
        self.running, self.waiting, self.total = {}, 0, 0
        self.events = {}
        self.max_queue, self.max_concurrency = max_queue, max_concurrency
        self.fenced = False
        self.closing, self.inflight, self.signals = False, 0, {}

    def begin_shutdown(self):
        with self.condition:
            self.closing = True
            for signal in self.signals.values():
                signal.set()
            self.condition.notify_all()

    def shutdown(self):
        """Fence new work and drain owned calls before releasing the state lease."""
        self.begin_shutdown()
        with self.condition:
            while self.inflight:
                self.condition.wait(0.1)

    @staticmethod
    def _authorize(authority, request, config):
        grants = authority.grants
        if (
            "infer" not in grants.permissions
            or request.alias not in grants.aliases
            or request.operation not in grants.operations
        ):
            raise EngineError("inference_forbidden")
        if grants.run_id is not None and request.context.run_id != grants.run_id:
            raise EngineError("run_binding_mismatch")
        if grants.phase is not None and request.context.phase != grants.phase:
            raise EngineError("phase_binding_mismatch")
        profile = config.aliases.get(request.alias)
        if (
            profile
            and config.mode == "live"
            and profile.pricing
            and not profile.pricing.free_only
            and grants.run_id is None
            and not (grants.allow_client_run_ids and request.context.run_id is not None)
            and not authority.owner_ipc
        ):
            raise EngineError("paid_run_binding_required")

    def owned_request(self, authority, request_id):
        row = self.ledger.request(authority.project, authority.principal, request_id)
        context = json.loads(row["context_json"])
        if any(
            getattr(authority.grants, name) is not None
            and getattr(authority.grants, name) != context.get(name)
            for name in ("run_id", "phase")
        ):
            raise LedgerError("request_unknown")
        return row

    @exact_money
    def receipts(self, authority, request_id):
        row = self.owned_request(authority, request_id)
        snapshot = self.registry.get(authority.project, row["config_digest"])
        alias = snapshot.config.aliases[row["alias"]]
        receipts = []
        for attempt in self.ledger.attempts(authority.project, authority.principal, request_id):
            route = json.loads(attempt["route_json"])
            model = (
                ModelIdentity.model_validate_json(attempt["model_json"])
                if attempt["model_json"]
                else ModelIdentity(
                    alias=row["alias"],
                    provider_ref=route["provider_ref"],
                    configured_model=alias.upstream_model,
                    upstream_model=route["upstream_model"],
                )
            )
            protected = Decimal(attempt["protected_usd"])
            if attempt["generation_id"] is not None:
                model = model.model_copy(update={"generation_id": attempt["generation_id"]})
            known = Decimal(attempt["measured_usd"] or attempt["estimated_usd"] or "0")
            receipts.append(
                AttemptReceipt(
                    receipt_id=attempt["attempt_id"],
                    project_id=authority.project,
                    principal_id=authority.principal,
                    request_id=request_id,
                    attempt_id=attempt["attempt_id"],
                    attempt=attempt["ordinal"],
                    config_digest=row["config_digest"],
                    model=model,
                    usage=TokenUsage.model_validate_json(attempt["usage_json"])
                    if attempt["usage_json"]
                    else TokenUsage(),
                    evidence_status="not_dispatched"
                    if attempt["evidence_status"] == "no_charge"
                    else attempt["evidence_status"],
                    measured_usd=attempt["measured_usd"],
                    estimated_usd=attempt["estimated_usd"],
                    protected_debit_usd=protected,
                    contingent_exposure_usd=max(Decimal("0"), protected - known),
                    pricing_source=route.get("pricing_source"),
                    reservation_kind=route.get(
                        "reservation_kind",
                        "legacy_unclassified" if row["operation"] == "x_search" else "bound",
                    ),
                    usage_complete=bool(attempt["usage_complete"]),
                    reconciliation_required=bool(
                        attempt["unknown_hold"] or attempt["evidence_status"] == "estimated"
                    ),
                )
            )
        return receipts

    def lookup(self, authority, request_id):
        row = self.owned_request(authority, request_id)
        retained = self.results.get(authority.project, authority.principal, request_id)
        if retained and row["execution_status"] == retained.execution_status:
            receipts = self.receipts(authority, request_id)
            return retained.model_copy(
                update={
                    "receipts": receipts,
                    "model": receipts[-1].model if receipts else retained.model,
                }
            )
        snapshot = self.registry.get(authority.project, row["config_digest"])
        profile = snapshot.config.aliases[row["alias"]]
        receipts = self.receipts(authority, request_id)
        return InferenceResult(
            request_id=request_id,
            trace_id=row["trace_id"],
            project_id=authority.project,
            config_digest=row["config_digest"],
            execution_status=row["execution_status"],
            output_status="pending" if row["execution_status"] == "in_progress" else "unavailable",
            model=receipts[-1].model
            if receipts
            else ModelIdentity(
                alias=row["alias"],
                provider_ref=profile.provider_ref,
                configured_model=profile.upstream_model,
                upstream_model=profile.upstream_model,
            ),
            receipts=receipts,
            provider_metadata={
                "result_available": False,
                "recorded_output_status": row["output_status"],
            },
        )

    def cancel(self, authority, request_id):
        self.owned_request(authority, request_id)
        self.ledger.cancel(authority.project, authority.principal, request_id)
        with self.condition:
            event = self.events.get((authority.project, authority.principal, request_id))
            if event:
                event.set()
            self.condition.notify_all()

    def run(
        self,
        authority,
        request,
        *,
        cancel_signal=None,
        received_at=None,
        expected_config_digest=None,
    ):
        if expected_config_digest is not None and (
            not isinstance(expected_config_digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected_config_digest)
        ):
            raise EngineError("expected_configuration_digest_invalid")
        signal = cancel_signal or threading.Event()
        signal_identity = object()
        with self.condition:
            if self.closing:
                raise EngineError("service_shutting_down")
            if self.fenced:
                raise EngineError("ledger_unavailable")
            self.inflight += 1
            self.signals[signal_identity] = signal
        try:
            bindings = {}
            if request.context.run_id is None and authority.grants.run_id is not None:
                bindings["run_id"] = authority.grants.run_id
            if (
                "phase" not in request.context.model_fields_set
                and authority.grants.phase is not None
            ):
                bindings["phase"] = authority.grants.phase
            if bindings:
                request = request.model_copy(
                    update={"context": request.context.model_copy(update=bindings)}
                )
            return self._run(
                authority,
                request,
                cancel_signal=signal,
                received_at=received_at,
                expected_config_digest=expected_config_digest,
            )
        except (OSError, sqlite3.Error):
            self.fenced = True
            raise EngineError("ledger_unavailable_query_receipt") from None
        finally:
            with self.condition:
                self.signals.pop(signal_identity, None)
                self.inflight -= 1
                self.condition.notify_all()

    def _run(
        self,
        authority,
        request,
        *,
        cancel_signal=None,
        received_at=None,
        expected_config_digest=None,
    ):
        snapshot = self.registry.get(authority.project)
        # Existing IDs always retain the original snapshot; authorization still
        # binds project/principal, and the semantic fingerprint must match.
        try:
            prior = self.ledger.request(authority.project, authority.principal, request.request_id)
        except LedgerError:
            prior = None
        if prior is None:
            if expected_config_digest is not None and expected_config_digest != snapshot.digest:
                raise EngineError("expected_configuration_conflict")
            if snapshot.capability_revision != CAPABILITY_REVISION:
                raise EngineError("configuration_capability_unavailable_reload_required")
            if authority.config_digest is not None and authority.config_digest != snapshot.digest:
                raise EngineError("configuration_changed_before_admission")
            self._authorize(authority, request, snapshot.config)
        row, created = self.ledger.register(
            authority.project, authority.principal, request, snapshot, now=received_at
        )
        if not created:
            return self.lookup(authority, request.request_id)
        config, alias = snapshot.config, snapshot.config.aliases[request.alias]
        deadline = time.monotonic() + max(
            0, (datetime.fromisoformat(row["deadline_at"]) - datetime.now(UTC)).total_seconds()
        )
        key, event = (
            (authority.project, authority.principal, request.request_id),
            cancel_signal or threading.Event(),
        )
        alias_key = (authority.project, request.alias)
        acquired = False
        with self.condition:
            self.events[key] = event
        if self.ledger.request(authority.project, authority.principal, request.request_id)[
            "cancelled"
        ]:
            event.set()
        try:
            with self.condition:
                if self.waiting >= self.max_queue:
                    raise EngineError("queue_full")
                self.waiting += 1
                try:
                    while (
                        self.running.get(alias_key, 0) >= alias.limits.max_concurrency
                        or self.total >= self.max_concurrency
                    ):
                        if event.is_set() or time.monotonic() >= deadline:
                            raise EngineError("cancelled_or_deadline")
                        self.condition.wait(min(0.1, deadline - time.monotonic()))
                    self.running[alias_key] = self.running.get(alias_key, 0) + 1
                    self.total += 1
                    acquired = True
                finally:
                    self.waiting -= 1
            cached = self.results.cached(authority.project, authority.principal, request, snapshot)
            if cached:
                if (
                    request.operation == "x_search"
                    and self.validation.cached_x(
                        cached,
                        request.input,
                        alias,
                        config.operation_policies[alias.operation_policy_ref],
                        remaining=max(0, deadline - time.monotonic()),
                        cancel=event,
                    ).status
                    != "valid"
                ):
                    raise EngineError("quota_cache_conflict")
                attempt = self.ledger.admit(
                    authority.project,
                    authority.principal,
                    request.request_id,
                    ordinal=1,
                    route={
                        "provider_ref": alias.provider_ref,
                        "upstream_model": alias.upstream_model,
                        "pricing_source": "cached_reuse",
                    },
                    reserve=Decimal("0"),
                    buckets=[],
                    max_attempts=1,
                )
                self.ledger.settle(
                    authority.project,
                    attempt,
                    status="cached_reuse",
                    model=cached.model.model_dump(mode="json"),
                    output_status="valid",
                )
                output = Output(
                    "valid",
                    cached.content,
                    cached.function_calls,
                    cached.finish_reason,
                    {
                        **cached.provider_metadata,
                        "reused_receipt_ids": [r.receipt_id for r in cached.receipts],
                        "original_charge_receipt_ids": cached.provider_metadata.get(
                            "original_charge_receipt_ids", [r.receipt_id for r in cached.receipts]
                        ),
                    },
                )
                return self._finish(authority, request, row, snapshot, output, cached.model)
            maximum = min(alias.attempt_policy.max_attempts, request.limits.max_attempts or 10)
            output, model = Output("unavailable"), None
            for ordinal in range(1, maximum + 1):
                if event.is_set() or time.monotonic() >= deadline:
                    raise EngineError("cancelled_or_deadline")
                profile = alias
                if alias.attempt_policy.fallback_routes:
                    if ordinal > 1:
                        fallback = alias.attempt_policy.fallback_routes[
                            min(ordinal - 2, len(alias.attempt_policy.fallback_routes) - 1)
                        ]
                        profile = type(alias).model_validate(
                            {**alias.model_dump(mode="json"), **fallback.model_dump(mode="json")}
                        )
                provider = config.providers[profile.provider_ref]
                policy = config.operation_policies.get(profile.operation_policy_ref)
                prepared = self.validation.prepare(
                    profile,
                    provider,
                    request.input,
                    operation_policy=policy,
                    caller_limits=request.limits,
                    remaining=max(0, deadline - time.monotonic()),
                    cancel=event,
                )
                reserve = (
                    Decimal("0")
                    if config.mode == "fake"
                    else reserve_usd(
                        profile,
                        prepared.input_token_bound,
                        prepared.max_output_tokens,
                        provider_kind=provider.kind,
                    )
                )
                buckets = [
                    bind_bucket(
                        name,
                        config.budgets[name],
                        project_id=authority.project,
                        principal_id=authority.principal,
                        request_id=request.request_id,
                        provider_ref=profile.provider_ref,
                        alias=request.alias,
                        context=request.context,
                        timezone=config.timezone,
                        now=datetime.now(UTC),
                    )
                    for name in profile.budget_policy_refs
                ]
                attempt = self.ledger.admit(
                    authority.project,
                    authority.principal,
                    request.request_id,
                    ordinal=ordinal,
                    route={
                        "provider_ref": profile.provider_ref,
                        "upstream_model": profile.upstream_model,
                        "pricing_source": profile.pricing.revision
                        if profile.pricing
                        else "simulated",
                        "reservation_kind": "simulated"
                        if config.mode == "fake"
                        else "risk_allowance"
                        if request.operation == "x_search"
                        else "bound",
                    },
                    reserve=reserve,
                    buckets=buckets,
                    max_attempts=maximum,
                    strict=alias.attempt_policy.unknown_charge_policy == "stop",
                )
                result, failure = None, None
                try:
                    quota = (
                        (
                            profile.operation_policy_ref,
                            period_window("day", config.timezone, datetime.now(UTC)).key,
                        )
                        if request.operation == "x_search"
                        else None
                    )
                    self.ledger.dispatch_intent(authority.project, attempt, quota=quota)
                    if config.mode == "fake":
                        fixture = self.results.next_fixture(
                            snapshot, authority.principal, request.alias, request.context
                        )
                        try:
                            payload = json.loads(fixture)
                        except ValueError:
                            payload = None
                        if request.operation == "chat" and (
                            not isinstance(payload, dict) or "choices" not in payload
                        ):
                            payload = {
                                "choices": [
                                    {"finish_reason": "stop", "message": {"content": fixture}}
                                ]
                            }
                        if not isinstance(payload, dict):
                            raise ProviderFailure("fake_fixture_invalid", dispatched=False)
                        result = ProviderEvidence(payload, TokenUsage(), None, None, None, None)
                    else:
                        result = self.provider.invoke(
                            prepared,
                            provider,
                            cancel=event,
                            timeout=max(0.001, deadline - time.monotonic()),
                        )
                except ProviderFailure as error:
                    failure, result = error, error.evidence
                except LedgerError:
                    self.ledger.settle(authority.project, attempt, status="no_charge")
                    raise
                except ValueError:
                    failure = ProviderFailure("fake_fixture_unavailable", dispatched=False)
                model = ModelIdentity(
                    alias=request.alias,
                    provider_ref=profile.provider_ref,
                    configured_model=alias.upstream_model,
                    upstream_model=profile.upstream_model,
                    actual_model=result.actual_model if result else None,
                    actual_provider=result.actual_provider if result else None,
                    generation_id=result.generation_id if result else None,
                )
                output = (
                    self.validation.normalize(
                        result.payload,
                        request.input,
                        profile,
                        operation_policy=policy,
                        remaining=max(0, deadline - time.monotonic()),
                        cancel=event,
                        quota_period=quota[1] if request.operation == "x_search" else None,
                        request_identity=subset_identity(request)
                        if request.operation == "x_search"
                        else None,
                    )
                    if result and not failure
                    else Output("provider_failed")
                )
                evidence = dict(
                    usage=result.usage if result else TokenUsage(),
                    model=model.model_dump(mode="json"),
                    generation_id=result.generation_id if result else None,
                    output_status=output.status,
                    error_code=failure.code if failure else None,
                )
                if config.mode == "fake":
                    status = "simulated"
                elif failure and not failure.dispatched:
                    status = "no_charge"
                elif result and result.measured_usd is not None and not result.billing_anomaly:
                    status = "measured"
                    evidence["measured"] = result.measured_usd
                elif profile.pricing.free_only and not (result and result.billing_anomaly):
                    status = "explicit_free"
                elif (
                    result
                    and not result.billing_anomaly
                    and (
                        estimate := estimate_usd(
                            profile,
                            result.usage,
                        )
                    )
                    is not None
                ):
                    status = "estimated"
                    evidence["estimated"] = estimate
                else:
                    status = "unavailable"
                self.ledger.settle(authority.project, attempt, status=status, **evidence)
                if "validation_diagnostic" in output.metadata:
                    break
                if (
                    event.is_set()
                    or failure
                    and failure.code in {"provider_cancelled", "provider_deadline_exceeded"}
                ):
                    break
                if (
                    output.status in {"valid", "invalid_tool_arguments"}
                    or status == "unavailable"
                    and alias.attempt_policy.unknown_charge_policy == "stop"
                ):
                    break
                if ordinal < maximum and event.wait(
                    min(alias.attempt_policy.backoff_seconds, max(0, deadline - time.monotonic()))
                ):
                    break
            execution = (
                "cancelled"
                if event.is_set()
                else "timed_out"
                if time.monotonic() >= deadline
                else None
            )
            return self._finish(
                authority, request, row, snapshot, output, model, execution=execution
            )
        except (ValueError, EngineError) as error:
            self.ledger.finish(
                authority.project,
                authority.principal,
                request.request_id,
                execution_status="cancelled"
                if event.is_set()
                else "timed_out"
                if time.monotonic() >= deadline
                else "failed",
                output_status="unavailable",
            )
            raise EngineError(str(error)) from None
        finally:
            with self.condition:
                self.events.pop(key, None)
                if acquired:
                    self.running[alias_key] -= 1
                    self.total -= 1
                self.condition.notify_all()

    def _finish(self, authority, request, row, snapshot, output, model, *, execution=None):
        receipts = self.receipts(authority, request.request_id)
        attempts = self.ledger.attempts(authority.project, authority.principal, request.request_id)
        errors = [a["error_code"] for a in attempts if a["error_code"]]
        if errors:
            output.metadata["attempt_errors"] = errors
        execution = execution or ("completed" if output.status == "valid" else "failed")
        result = InferenceResult(
            request_id=request.request_id,
            trace_id=row["trace_id"],
            project_id=authority.project,
            config_digest=snapshot.digest,
            execution_status=execution,
            output_status=output.status,
            content=output.content,
            function_calls=output.function_calls,
            model=model,
            receipts=receipts,
            finish_reason=output.finish_reason,
            provider_metadata=output.metadata,
            error=None
            if output.status == "valid" and execution == "completed"
            else SafeError(
                code=errors[-1]
                if errors
                else execution
                if execution in {"cancelled", "timed_out"}
                else output.status,
                message="Inference output unavailable or invalid; query receipt.",
                advice="query_receipt",
                trace_id=row["trace_id"],
                receipt_id=receipts[-1].receipt_id if receipts else None,
            ),
        )
        with self.store.transaction() as connection:
            reference = self.results.retain(
                result, authority.principal, request, snapshot, connection=connection
            )
            self.ledger.finish(
                authority.project,
                authority.principal,
                request.request_id,
                execution_status=execution,
                output_status=output.status,
                result_ref=reference,
                connection=connection,
            )
        return result
