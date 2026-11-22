"""Opt-in private content retention; expiry never deletes monetary tombstones."""

import hashlib
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

from llm_gateway.config.loader import canonical_json
from llm_gateway.contracts import InferenceResult

from .ledger import fingerprint
from .periods import period_window
from .sqlite import StoreError


def cache_key(request, snapshot):
    return hashlib.sha256((snapshot.digest + ":" + fingerprint(request)).encode()).hexdigest()


class Results:
    def __init__(self, store):
        self.store = store

    def retain(
        self,
        result: InferenceResult,
        principal: str,
        request,
        snapshot,
        *,
        now=None,
        connection=None,
    ):
        policy = snapshot.config.evidence_policy
        if not policy.retain_results and not policy.retain_prompts:
            return None
        now = now or datetime.now(UTC)
        payload = result.model_dump_json()
        if len(payload.encode()) > 16 * 1024 * 1024:
            raise StoreError("result_retention_size_limit")
        transaction = (
            nullcontext(connection) if connection is not None else self.store.transaction()
        )
        with transaction as connection:
            if not connection.in_transaction:
                raise StoreError("result_transaction_required")
            if policy.retain_prompts:
                prompt = request.input.model_dump_json()
                if len(prompt.encode()) > 16 * 1024 * 1024:
                    raise StoreError("prompt_retention_size_limit")
                connection.execute(
                    "INSERT INTO retained_prompts VALUES (?,?,?,?,?)",
                    (
                        result.project_id,
                        principal,
                        result.request_id,
                        prompt,
                        (now + timedelta(seconds=policy.result_ttl_seconds)).isoformat(),
                    ),
                )
            if not policy.retain_results:
                return None
            connection.execute(
                "INSERT INTO retained_results VALUES (?,?,?,?,?,?,?)",
                (
                    result.project_id,
                    principal,
                    result.request_id,
                    snapshot.digest,
                    cache_key(request, snapshot),
                    payload,
                    (now + timedelta(seconds=policy.result_ttl_seconds)).isoformat(),
                ),
            )
        return result.request_id

    def get(self, project, principal, request_id, *, now=None):
        now = now or datetime.now(UTC)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE retained_prompts SET payload=NULL WHERE project_id=? AND expires_at<=?",
                (project, now.isoformat()),
            )
            connection.execute(
                "UPDATE retained_results SET payload=NULL WHERE project_id=? AND expires_at<=?",
                (project, now.isoformat()),
            )
            row = connection.execute(
                "SELECT payload FROM retained_results WHERE project_id=? AND principal_id=? "
                "AND request_id=?",
                (project, principal, request_id),
            ).fetchone()
            return InferenceResult.model_validate_json(row[0]) if row and row[0] else None

    def _complete_evidence(self, result, project, principal):
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT usage_complete,unknown_hold FROM attempts "
                "WHERE project_id=? AND principal_id=? AND request_id=?",
                (project, principal, result.request_id),
            ).fetchall()
        return (
            bool(rows)
            and len(rows) == len(result.receipts)
            and all(r[0] and not r[1] for r in rows)
        )

    def cached(self, project, principal, request, snapshot, *, now=None):
        if not snapshot.config.evidence_policy.cache_enabled:
            return None
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT request_id FROM retained_results WHERE project_id=? AND "
                "principal_id=? AND config_digest=? AND cache_key=? AND payload IS NOT NULL "
                "ORDER BY expires_at DESC",
                (project, principal, snapshot.digest, cache_key(request, snapshot)),
            ).fetchall()
        for row in rows:
            result = self.get(project, principal, row[0], now=now)
            if (
                result
                and result.output_status == "valid"
                and self._complete_evidence(result, project, principal)
            ):
                if request.operation == "x_search" and not self._current_x_scope(
                    result, snapshot, now
                ):
                    continue
                return result
        if request.operation == "x_search":
            return self._subset_x(project, principal, request, snapshot, now=now)
        return None

    @staticmethod
    def _current_x_scope(result, snapshot, now):
        return (
            result.provider_metadata.get("scope_verified") is True
            and result.provider_metadata.get("x_scope", {}).get("quota_period")
            == period_window("day", snapshot.config.timezone, now or datetime.now(UTC)).key
        )

    def _subset_x(self, project, principal, request, snapshot, *, now):
        from llm_gateway.service.x_scope import subset_identity, validate_posts

        policy = snapshot.config.operation_policies[
            snapshot.config.aliases[request.alias].operation_policy_ref
        ]
        if not policy.allow_subset_cache_reuse:
            return None
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT request_id FROM retained_results WHERE project_id=? AND principal_id=? "
                "AND config_digest=? AND payload IS NOT NULL ORDER BY expires_at DESC",
                (project, principal, snapshot.digest),
            ).fetchall()
        wanted = {h.casefold() for h in request.input.allowed_x_handles}
        for row in rows:
            result = self.get(project, principal, row[0], now=now)
            if (
                not result
                or not self._current_x_scope(result, snapshot, now)
                or result.output_status != "valid"
                or not self._complete_evidence(result, project, principal)
            ):
                continue
            scope = result.provider_metadata.get("x_scope", {})
            if scope.get("subset_identity") != subset_identity(request) or not wanted.issubset(
                set(scope.get("handles", []))
            ):
                continue
            document = {
                **result.content,
                "posts": [
                    post
                    for post in result.content["posts"]
                    if post["author_handle"].lstrip("@").casefold() in wanted
                ],
            }
            try:
                validate_posts(document, request.input, policy, schema_validation=False)
            except Exception:
                continue
            metadata = {
                **result.provider_metadata,
                "original_scope": result.provider_metadata.get("original_scope", scope),
                "x_scope": {**scope, "handles": sorted(wanted)},
                "served_scope": sorted(wanted),
            }
            return result.model_copy(update={"content": document, "provider_metadata": metadata})
        return None

    def next_fixture(self, snapshot, principal, alias, context):
        config = snapshot.config
        stage = context.stage_id or "default"
        paths = config.fake.responses.get(alias, {}).get(stage, []) if config.fake else []
        if not paths:
            raise StoreError("fake_fixture_unavailable")
        key = canonical_json([alias, context.session_id, context.run_id, stage])
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT position FROM fake_sequences WHERE project_id=? AND principal_id=? "
                "AND config_digest=? AND sequence_key=?",
                (snapshot.project_id, principal, snapshot.digest, key),
            ).fetchone()
            position = row[0] if row else 0
            if position >= len(paths):
                raise StoreError("fake_sequence_exhausted")
            connection.execute(
                "INSERT INTO fake_sequences VALUES (?,?,?,?,?) ON "
                "CONFLICT(project_id,principal_id,config_digest,sequence_key) DO UPDATE SET "
                "position=excluded.position",
                (snapshot.project_id, principal, snapshot.digest, key, position + 1),
            )
        return snapshot.fixtures[paths[position]]["content"]
