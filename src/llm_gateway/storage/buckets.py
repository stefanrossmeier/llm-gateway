"""T026: stable scoped bucket identities, independent of alias/config revision."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from llm_gateway.config.models import BudgetPolicy
from llm_gateway.contracts import RequestContext, parse_usd, usd_string

from .periods import period_window
from .sqlite import StoreError


@dataclass(frozen=True)
class BudgetBucket:
    bucket_id: str
    project_id: str
    policy_id: str
    scope_id: str
    period_key: str
    start_at: str | None
    end_at: str | None
    cap_usd: Decimal


def bind_bucket(
    policy_id: str,
    policy: BudgetPolicy,
    *,
    project_id: str,
    principal_id: str,
    request_id: str,
    provider_ref: str,
    alias: str,
    context: RequestContext,
    timezone: str,
    now: datetime,
) -> BudgetBucket:
    scope = {
        "project": project_id,
        "credential": provider_ref,
        "run": context.run_id,
        "workflow": context.workflow_id,
        "alias": alias,
    }[policy.scope]
    if not scope:
        raise StoreError("budget_scope_unbound")
    period_scope = None
    if policy.period == "call":
        period_scope = [principal_id, request_id]
    elif policy.period == "run":
        if not context.run_id:
            raise StoreError("budget_run_unbound")
        period_scope = context.run_id
    scope_id = json.dumps([policy.scope, scope, period_scope], separators=(",", ":"))
    window = period_window(policy.period, timezone, now)
    identity = json.dumps([project_id, policy_id, scope_id, window.key], separators=(",", ":"))
    return BudgetBucket(
        hashlib.sha256(identity.encode()).hexdigest(),
        project_id,
        policy_id,
        scope_id,
        window.key,
        window.start.isoformat() if window.start else None,
        window.end.isoformat() if window.end else None,
        policy.cap_usd,
    )


def ensure_bucket(connection: sqlite3.Connection, bucket: BudgetBucket) -> Decimal:
    if not connection.in_transaction:
        raise StoreError("bucket_transaction_required")
    connection.execute(
        "INSERT INTO budget_buckets "
        "(bucket_id,project_id,policy_id,scope_id,period_key,start_at,end_at,cap_usd) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(bucket_id) DO UPDATE SET cap_usd=excluded.cap_usd",
        (
            bucket.bucket_id,
            bucket.project_id,
            bucket.policy_id,
            bucket.scope_id,
            bucket.period_key,
            bucket.start_at,
            bucket.end_at,
            usd_string(bucket.cap_usd),
        ),
    )
    row = connection.execute(
        "SELECT protected_usd FROM budget_buckets WHERE bucket_id=?", (bucket.bucket_id,)
    ).fetchone()
    return parse_usd(row[0])
