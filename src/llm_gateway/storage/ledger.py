"""Transactional admission, dispatch intent, exact-money settlement and recovery.

Provider I/O is deliberately absent. Each mutating method commits its monetary
transition and a sanitized trace event in the same SQLite transaction.
"""

import hashlib
import json
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from functools import wraps
from zoneinfo import ZoneInfo

from llm_gateway.config.loader import ConfigSnapshot, canonical_json
from llm_gateway.contracts import InferenceRequest, TokenUsage, parse_usd, usd_string

from .buckets import BudgetBucket, ensure_bucket
from .sqlite import StateStore, StoreError


class LedgerError(StoreError):
    pass


def exact_money(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = 50
            return function(*args, **kwargs)

    return wrapped


def fingerprint(request: InferenceRequest) -> str:
    body = request.model_dump(mode="json", exclude={"request_id"})
    return hashlib.sha256(canonical_json(body).encode()).hexdigest()


def _event(connection, project, request, event, **metadata):
    connection.execute(
        "INSERT INTO trace_outbox(project_id,request_id,event_json) VALUES (?,?,?)",
        (project, request, canonical_json({"event": event, **metadata})),
    )


def _attempt(connection, project, attempt_id):
    row = connection.execute(
        "SELECT * FROM attempts WHERE project_id=? AND attempt_id=?", (project, attempt_id)
    ).fetchone()
    if row is None:
        raise LedgerError("attempt_unknown")
    return row


def _move_protected(connection, row, amount):
    allocations = connection.execute(
        "SELECT a.*, b.protected_usd AS bucket_total FROM allocations a "
        "JOIN budget_buckets b USING(bucket_id) WHERE attempt_id=?",
        (row["attempt_id"],),
    ).fetchall()
    for allocation in allocations:
        total = Decimal(allocation["bucket_total"]) - Decimal(allocation["protected_usd"]) + amount
        if total < 0:
            raise LedgerError("ledger_invariant_failed")
        connection.execute(
            "UPDATE budget_buckets SET protected_usd=? WHERE bucket_id=?",
            (usd_string(parse_usd(total)), allocation["bucket_id"]),
        )
        connection.execute(
            "UPDATE allocations SET protected_usd=? WHERE attempt_id=? AND bucket_id=?",
            (usd_string(amount), row["attempt_id"], allocation["bucket_id"]),
        )


def _settle(
    connection,
    row,
    *,
    status,
    measured=None,
    estimated=None,
    usage=None,
    model=None,
    generation_id=None,
    output_status="unavailable",
    error_code=None,
    terminal_state="settled",
):
    if (measured is not None and status != "measured") or (
        estimated is not None and status != "estimated"
    ):
        raise LedgerError("evidence_status_conflict")
    if status == "measured":
        amount = parse_usd(measured)
    elif status == "estimated":
        amount = max(Decimal(row["reserve_usd"]), parse_usd(estimated))
    elif status in {"explicit_free", "simulated", "cached_reuse", "no_charge"}:
        amount = Decimal("0")
    elif status == "unavailable":
        amount = Decimal(row["reserve_usd"])
    else:
        raise LedgerError("evidence_status_invalid")
    overrun = amount > Decimal(row["reserve_usd"])
    if overrun and Decimal(row["reserve_usd"]) == 0:
        connection.execute(
            "INSERT INTO runtime_flags VALUES (?,?) ON CONFLICT(name) "
            "DO UPDATE SET value=excluded.value",
            ("project_fenced:" + row["project_id"], "unexpected_zero_reserve_charge"),
        )
    if overrun and json.loads(row["route_json"]).get("reservation_kind") == "risk_allowance":
        connection.execute(
            "INSERT INTO runtime_flags VALUES (?,?) ON CONFLICT(name) "
            "DO UPDATE SET value=excluded.value",
            ("project_fenced:" + row["project_id"], "native_x_allowance_overrun"),
        )
    hold = status == "unavailable" and amount > 0 or overrun
    _move_protected(connection, row, amount)
    connection.execute(
        "UPDATE attempts SET state=?,protected_usd=?,measured_usd=?,estimated_usd=?,"
        "evidence_status=?,usage_json=?,model_json=?,generation_id=?,error_code=?,"
        "output_status=?,usage_complete=?,unknown_hold=? WHERE attempt_id=?",
        (
            terminal_state,
            usd_string(amount),
            usd_string(measured) if measured is not None else None,
            usd_string(estimated) if estimated is not None else None,
            status,
            (usage or TokenUsage()).model_dump_json(),
            canonical_json(model) if model else row["model_json"],
            generation_id or row["generation_id"],
            error_code,
            output_status,
            int(status in {"measured", "simulated", "cached_reuse", "no_charge"}),
            int(hold),
            row["attempt_id"],
        ),
    )
    _event(
        connection,
        row["project_id"],
        row["request_id"],
        "attempt_settled",
        attempt_id=row["attempt_id"],
        evidence_status=status,
        protected_usd=usd_string(amount),
        overrun=overrun,
    )


class Ledger:
    def __init__(self, store: StateStore):
        self.store = store

    def acknowledge_allowance(self, project, attempt_id, *, allowance, evidence_id, actor, reason):
        """Explicit operator review; never removes unknown charge or restore fences."""
        payload = canonical_json(
            {
                "attempt_id": attempt_id,
                "allowance": usd_string(allowance),
                "actor": actor,
                "reason": reason,
            }
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.store.transaction() as connection:
            previous = connection.execute(
                "SELECT digest FROM evidence WHERE project_id=? AND evidence_id=?",
                (project, evidence_id),
            ).fetchone()
            if previous:
                if previous[0] != digest:
                    raise LedgerError("evidence_id_conflict")
                return False
            row = _attempt(connection, project, attempt_id)
            if (
                json.loads(row["route_json"]).get("reservation_kind") != "risk_allowance"
                or row["state"] != "settled"
                or row["measured_usd"] is None
                or Decimal(row["measured_usd"]) <= Decimal(row["reserve_usd"])
                or allowance < Decimal(row["measured_usd"])
            ):
                raise LedgerError("allowance_review_invalid")
            connection.execute(
                "UPDATE attempts SET unknown_hold=0 WHERE attempt_id=?", (attempt_id,)
            )
            connection.execute(
                "INSERT INTO evidence VALUES (?,?,?,?,?,?)",
                (project, evidence_id, attempt_id, digest, payload, datetime.now(UTC).isoformat()),
            )
            unresolved = connection.execute(
                "SELECT 1 FROM attempts WHERE project_id=? AND unknown_hold=1 "
                "AND json_extract(route_json,'$.reservation_kind')='risk_allowance' LIMIT 1",
                (project,),
            ).fetchone()
            if not unresolved:
                connection.execute(
                    "DELETE FROM runtime_flags WHERE name=? AND value='native_x_allowance_overrun'",
                    ("project_fenced:" + project,),
                )
            return True

    def register(
        self,
        project: str,
        principal: str,
        request: InferenceRequest,
        snapshot: ConfigSnapshot,
        *,
        now: datetime | None = None,
    ) -> tuple[dict, bool]:
        if snapshot.project_id != project:
            raise LedgerError("project_snapshot_mismatch")
        now = now or datetime.now(UTC)
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM requests WHERE project_id=? AND principal_id=? AND request_id=?",
                (project, principal, request.request_id),
            ).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint(request):
                    raise LedgerError("idempotency_conflict")
                return dict(existing), False
            profile = snapshot.config.aliases.get(request.alias)
            if profile is None or profile.operation != request.operation:
                raise LedgerError("alias_operation_forbidden")
            timeout = min(
                profile.limits.request_timeout_seconds,
                request.limits.timeout_seconds or profile.limits.request_timeout_seconds,
            )
            connection.execute(
                "INSERT INTO requests "
                "(project_id,principal_id,request_id,fingerprint,config_digest,"
                "alias,operation,context_json,limits_json,trace_id,created_at,deadline_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    project,
                    principal,
                    request.request_id,
                    fingerprint(request),
                    snapshot.digest,
                    request.alias,
                    request.operation,
                    request.context.model_dump_json(),
                    request.limits.model_dump_json(),
                    uuid.uuid4().hex,
                    now.isoformat(),
                    (now + timedelta(seconds=timeout)).isoformat(),
                ),
            )
            _event(
                connection,
                project,
                request.request_id,
                "request_registered",
                config_digest=snapshot.digest,
            )
            row = connection.execute(
                "SELECT * FROM requests WHERE project_id=? AND principal_id=? AND request_id=?",
                (project, principal, request.request_id),
            ).fetchone()
            return dict(row), True

    def request(self, project: str, principal: str, request_id: str) -> dict:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE project_id=? AND principal_id=? AND request_id=?",
                (project, principal, request_id),
            ).fetchone()
        if row is None:
            raise LedgerError("request_unknown")
        return dict(row)

    @exact_money
    def admit(
        self,
        project: str,
        principal: str,
        request_id: str,
        *,
        ordinal: int,
        route: dict,
        reserve: Decimal,
        buckets: list[BudgetBucket],
        max_attempts: int,
        strict: bool = True,
    ) -> str:
        reserve = parse_usd(reserve)
        if ordinal < 1 or ordinal > min(max_attempts, 10):
            raise LedgerError("attempt_limit")
        if len({b.bucket_id for b in buckets}) != len(buckets) or any(
            b.project_id != project for b in buckets
        ):
            raise LedgerError("bucket_identity_invalid")
        if reserve > 0 and not buckets:
            raise LedgerError("paid_budget_required")
        attempt_id = uuid.uuid4().hex
        with self.store.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM runtime_flags WHERE name IN ('admission_fenced',?)",
                ("project_fenced:" + project,),
            ).fetchone():
                raise LedgerError("admission_fenced")
            row = connection.execute(
                "SELECT * FROM requests WHERE project_id=? AND principal_id=? AND request_id=?",
                (project, principal, request_id),
            ).fetchone()
            if row is None or row["execution_status"] != "in_progress" or row["cancelled"]:
                raise LedgerError("request_not_dispatchable")
            if datetime.now(UTC) >= datetime.fromisoformat(row["deadline_at"]):
                raise LedgerError("request_deadline_exceeded")
            previous = connection.execute(
                "SELECT * FROM attempts WHERE project_id=? AND principal_id=? AND "
                "request_id=? ORDER BY ordinal",
                (project, principal, request_id),
            ).fetchall()
            if ordinal != len(previous) + 1 or any(
                a["state"] in {"reserved", "dispatch_intent"} for a in previous
            ):
                raise LedgerError("attempt_sequence_conflict")
            limits = json.loads(row["limits_json"])
            if limits.get("max_attempts") is not None and ordinal > limits["max_attempts"]:
                raise LedgerError("attempt_limit")
            if limits.get("max_usd") is not None and sum(
                (Decimal(a["protected_usd"]) for a in previous), Decimal("0")
            ) + reserve > Decimal(limits["max_usd"]):
                raise LedgerError("caller_budget_exceeded")
            for bucket in buckets:
                total = ensure_bucket(connection, bucket)
                if (
                    strict
                    and reserve > 0
                    and connection.execute(
                        "SELECT 1 FROM allocations a JOIN attempts t USING(attempt_id) WHERE "
                        "a.bucket_id=? AND t.unknown_hold=1 LIMIT 1",
                        (bucket.bucket_id,),
                    ).fetchone()
                ):
                    raise LedgerError("unknown_billing_hold")
                if total + reserve > bucket.cap_usd:
                    raise LedgerError("budget_exceeded")
            connection.execute(
                "INSERT INTO attempts (attempt_id,project_id,principal_id,request_id,ordinal,"
                "state,route_json,reserve_usd,protected_usd) VALUES "
                "(?,?,?,?,?,'reserved',?,?,?)",
                (
                    attempt_id,
                    project,
                    principal,
                    request_id,
                    ordinal,
                    canonical_json(route),
                    usd_string(reserve),
                    usd_string(reserve),
                ),
            )
            for bucket in buckets:
                current = Decimal(
                    connection.execute(
                        "SELECT protected_usd FROM budget_buckets WHERE bucket_id=?",
                        (bucket.bucket_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    "UPDATE budget_buckets SET protected_usd=? WHERE bucket_id=?",
                    (usd_string(current + reserve), bucket.bucket_id),
                )
                connection.execute(
                    "INSERT INTO allocations VALUES (?,?,?)",
                    (attempt_id, bucket.bucket_id, usd_string(reserve)),
                )
            _event(
                connection,
                project,
                request_id,
                "attempt_reserved",
                attempt_id=attempt_id,
                ordinal=ordinal,
                reserve_usd=usd_string(reserve),
            )
        return attempt_id

    def dispatch_intent(
        self, project: str, attempt_id: str, *, quota: tuple[str, str] | None = None
    ):
        with self.store.transaction() as connection:
            row = _attempt(connection, project, attempt_id)
            if row["state"] != "reserved":
                raise LedgerError("dispatch_already_claimed")
            request = connection.execute(
                "SELECT * FROM requests WHERE project_id=? AND principal_id=? AND request_id=?",
                (project, row["principal_id"], row["request_id"]),
            ).fetchone()
            if request["cancelled"] or datetime.now(UTC) >= datetime.fromisoformat(
                request["deadline_at"]
            ):
                raise LedgerError("request_not_dispatchable")
            if quota:
                existing = connection.execute(
                    "SELECT 1 FROM quota_slots WHERE project_id=? AND policy_id=? AND period_key=?",
                    (project, *quota),
                ).fetchone()
                if existing:
                    raise LedgerError("daily_quota_consumed")
                connection.execute(
                    "INSERT INTO quota_slots VALUES (?,?,?,?,?)",
                    (project, *quota, row["request_id"], attempt_id),
                )
            connection.execute(
                "UPDATE attempts SET state='dispatch_intent' WHERE attempt_id=? AND "
                "state='reserved'",
                (attempt_id,),
            )
            _event(connection, project, row["request_id"], "dispatch_intent", attempt_id=attempt_id)

    @exact_money
    def settle(self, project: str, attempt_id: str, *, status: str, **evidence):
        with self.store.transaction() as connection:
            row = _attempt(connection, project, attempt_id)
            if row["state"] not in {"reserved", "dispatch_intent"}:
                raise LedgerError("attempt_already_settled")
            if row["state"] == "reserved" and status not in {
                "no_charge",
                "simulated",
                "cached_reuse",
            }:
                raise LedgerError("settlement_without_intent")
            _settle(connection, row, status=status, **evidence)

    def finish(
        self,
        project,
        principal,
        request_id,
        *,
        execution_status,
        output_status,
        result_ref=None,
        connection=None,
    ):
        if execution_status not in {"completed", "failed", "cancelled", "timed_out"}:
            raise LedgerError("execution_status_invalid")
        transaction = (
            nullcontext(connection) if connection is not None else self.store.transaction()
        )
        with transaction as connection:
            if connection.execute(
                "SELECT 1 FROM attempts WHERE project_id=? AND principal_id=? AND "
                "request_id=? AND state IN ('reserved','dispatch_intent')",
                (project, principal, request_id),
            ).fetchone():
                raise LedgerError("attempt_not_settled")
            changed = connection.execute(
                "UPDATE requests SET execution_status=?,output_status=?,result_ref=? WHERE "
                "project_id=? AND principal_id=? AND request_id=? AND "
                "execution_status='in_progress'",
                (execution_status, output_status, result_ref, project, principal, request_id),
            ).rowcount
            if changed:
                _event(
                    connection,
                    project,
                    request_id,
                    "request_finished",
                    execution_status=execution_status,
                    output_status=output_status,
                )

    def cancel(self, project, principal, request_id):
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT 1 FROM requests WHERE project_id=? AND principal_id=? AND request_id=?",
                (project, principal, request_id),
            ).fetchone()
            if not row:
                raise LedgerError("request_unknown")
            connection.execute(
                "UPDATE requests SET cancelled=1 WHERE project_id=? AND principal_id=? AND "
                "request_id=?",
                (project, principal, request_id),
            )
            _event(connection, project, request_id, "cancellation_requested")

    def attempts(self, project, principal, request_id) -> list[dict]:
        self.request(project, principal, request_id)
        with self.store.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM attempts WHERE project_id=? AND principal_id=? AND "
                    "request_id=? ORDER BY ordinal",
                    (project, principal, request_id),
                )
            ]

    @exact_money
    def reconcile(
        self,
        project: str,
        evidence_id: str,
        *,
        attempt_id: str,
        request_id: str,
        generation_id: str,
        measured: Decimal,
        authority: str,
        observed_at: datetime,
    ) -> bool:
        measured = parse_usd(measured)
        if not authority or observed_at.tzinfo is None or len(evidence_id) > 128:
            raise LedgerError("evidence_invalid")
        payload = canonical_json(
            dict(
                attempt_id=attempt_id,
                request_id=request_id,
                generation_id=generation_id,
                measured_usd=usd_string(measured),
                authority=authority,
                observed_at=observed_at.isoformat(),
            )
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.store.transaction() as connection:
            old = connection.execute(
                "SELECT digest FROM evidence WHERE project_id=? AND evidence_id=?",
                (project, evidence_id),
            ).fetchone()
            if old:
                if old[0] != digest:
                    raise LedgerError("evidence_id_conflict")
                return False
            row = _attempt(connection, project, attempt_id)
            if (
                row["state"] != "settled"
                or row["request_id"] != request_id
                or not generation_id
                or (row["generation_id"] is not None and row["generation_id"] != generation_id)
            ):
                raise LedgerError("evidence_identity_mismatch")
            if row["evidence_status"] not in {
                "measured",
                "estimated",
                "unavailable",
                "explicit_free",
            }:
                raise LedgerError("evidence_reconciliation_forbidden")
            if row["measured_usd"] is not None and Decimal(row["measured_usd"]) != measured:
                raise LedgerError("measured_evidence_conflict")
            _settle(
                connection,
                row,
                status="measured",
                measured=measured,
                generation_id=generation_id,
                usage=TokenUsage.model_validate_json(row["usage_json"]),
                output_status=row["output_status"],
                error_code=row["error_code"],
            )
            connection.execute(
                "INSERT INTO evidence VALUES (?,?,?,?,?,?)",
                (project, evidence_id, attempt_id, digest, payload, observed_at.isoformat()),
            )
            return True

    @exact_money
    def report(
        self,
        project: str,
        *,
        principal: str | None = None,
        group_by: str | None = None,
        filters: dict[str, str] | None = None,
        timezone: str = "UTC",
    ) -> dict:
        allowed = {
            "alias",
            "principal_id",
            "operation",
            "run_id",
            "phase",
            "workflow_id",
            "stage_id",
            "profile",
            "purpose",
            "session_id",
            "role",
            "test_group",
            "provider_ref",
            "upstream_model",
            "actual_model",
            "actual_provider",
        }
        filters = dict(filters or {})
        windows = {}
        for key in ("since", "until"):
            if key in filters:
                try:
                    instant = datetime.fromisoformat(filters.pop(key).replace("Z", "+00:00"))
                    if instant.utcoffset() is None:
                        raise ValueError()
                    windows[key] = instant.astimezone(UTC)
                except (ValueError, TypeError):
                    raise LedgerError("report_time_invalid") from None
        if windows.get("since") and windows.get("until") and windows["since"] >= windows["until"]:
            raise LedgerError("report_time_invalid")
        if (group_by is not None and group_by not in allowed | {"day", "month"}) or set(
            filters
        ) - allowed:
            raise LedgerError("report_group_invalid")
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT a.*,r.alias,r.operation,r.context_json,r.created_at "
                "FROM attempts a JOIN requests "
                "r USING(project_id,principal_id,request_id) WHERE a.project_id=?"
                + (" AND a.principal_id=?" if principal else ""),
                (project, principal) if principal else (project,),
            ).fetchall()

            # A shared bucket's balance includes other callers/runs. Joining an
            # own allocation does not make that global balance own evidence.
            # Scoped reports expose only selected attempts; project-wide bucket
            # totals require unfiltered project reporting authority at the API.
            buckets = (
                connection.execute(
                    "SELECT * FROM budget_buckets WHERE project_id=?", (project,)
                ).fetchall()
                if principal is None and not filters and not windows
                else []
            )

        def dimension(row, name):
            if name in {"day", "month"}:
                day = datetime.fromisoformat(row["created_at"]).astimezone(ZoneInfo(timezone))
                return day.strftime("%Y-%m-%d" if name == "day" else "%Y-%m")
            if name in row.keys():
                return row[name]
            if name in {"provider_ref", "upstream_model"}:
                return json.loads(row["route_json"]).get(name)
            if name in {"actual_model", "actual_provider"}:
                return json.loads(row["model_json"] or "{}").get(name)
            return json.loads(row["context_json"]).get(name)

        rows = [
            r
            for r in rows
            if all(dimension(r, key) == value for key, value in filters.items())
            and (
                "since" not in windows
                or datetime.fromisoformat(r["created_at"]) >= windows["since"]
            )
            and (
                "until" not in windows or datetime.fromisoformat(r["created_at"]) < windows["until"]
            )
        ]

        def aggregate(items):
            measured = sum(
                (Decimal(r["measured_usd"]) for r in items if r["measured_usd"] is not None),
                Decimal("0"),
            )
            estimated = sum(
                (Decimal(r["estimated_usd"]) for r in items if r["estimated_usd"] is not None),
                Decimal("0"),
            )
            protected = sum((Decimal(r["protected_usd"]) for r in items), Decimal("0"))
            contingent = sum(
                (
                    max(
                        Decimal("0"),
                        Decimal(r["protected_usd"])
                        - Decimal(r["measured_usd"] or r["estimated_usd"] or "0"),
                    )
                    for r in items
                ),
                Decimal("0"),
            )
            return dict(
                attempts=len(items),
                measured_usd=usd_string(measured),
                estimated_usd=usd_string(estimated),
                protected_debit_usd=usd_string(protected),
                contingent_exposure_usd=usd_string(contingent),
                unknown_attempts=sum(
                    not r["usage_complete"] and r["evidence_status"] != "estimated" for r in items
                ),
                usage_complete=all(r["usage_complete"] for r in items),
                overrun=any(Decimal(r["protected_usd"]) > Decimal(r["reserve_usd"]) for r in items),
                measured_attempts=sum(r["evidence_status"] == "measured" for r in items),
                estimated_attempts=sum(r["evidence_status"] == "estimated" for r in items),
                unsettled_attempts=sum(r["state"] != "settled" for r in items),
            )

        result = aggregate(rows)
        result.update(project_id=project, currency="USD", timezone=timezone)
        result["attempt_ids"] = [r["attempt_id"] for r in rows]
        result["buckets"] = [
            dict(
                bucket_id=b["bucket_id"],
                policy_id=b["policy_id"],
                period_key=b["period_key"],
                cap_usd=b["cap_usd"],
                protected_debit_usd=b["protected_usd"],
                remaining_usd=usd_string(
                    max(Decimal("0"), Decimal(b["cap_usd"]) - Decimal(b["protected_usd"]))
                ),
            )
            for b in buckets
        ]
        if group_by:
            groups = {}
            for row in rows:
                value = dimension(row, group_by)
                groups.setdefault(value or "unbound", []).append(row)
            result["groups"] = {key: aggregate(items) for key, items in groups.items()}
        return result

    @exact_money
    def recover(self) -> int:
        """Startup only, after exclusive service ownership is acquired."""
        with self.store.transaction() as connection:
            pending = connection.execute(
                "SELECT * FROM attempts WHERE state IN ('reserved','dispatch_intent')"
            ).fetchall()
            for row in pending:
                _settle(
                    connection,
                    row,
                    status="no_charge" if row["state"] == "reserved" else "unavailable",
                    error_code="process_interrupted",
                )
            connection.execute(
                "UPDATE requests SET execution_status='failed',output_status='unavailable' "
                "WHERE execution_status='in_progress'"
            )
            return len(pending)
