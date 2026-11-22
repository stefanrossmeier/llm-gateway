"""T018/19: transactional immutable snapshots; failed reload never replaces active config."""

from pathlib import Path

from llm_gateway.storage import StateStore

from .loader import ConfigError, ConfigSnapshot, canonical_json, load_project_config


class ConfigRegistry:
    def __init__(self, store: StateStore):
        self.store = store

    @staticmethod
    def _snapshot(row) -> ConfigSnapshot:
        return ConfigSnapshot(
            row["project_id"],
            row["digest"],
            row["payload"],
            row["fixtures_payload"],
            row["capability_revision"],
        )

    def get(self, project_id: str, digest: str | None = None) -> ConfigSnapshot:
        with self.store.connection() as connection:
            if digest is None:
                row = connection.execute(
                    "SELECT s.* FROM config_snapshots s JOIN active_configs a "
                    "ON s.project_id=a.project_id AND s.digest=a.digest WHERE a.project_id=?",
                    (project_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM config_snapshots WHERE project_id=? AND digest=?",
                    (project_id, digest),
                ).fetchone()
        if row is None:
            raise ConfigError("project_snapshot_unknown")
        return self._snapshot(row)

    def activate(
        self,
        snapshot: ConfigSnapshot,
        *,
        expected_digest: str | None = None,
        compare_active: bool = False,
    ) -> None:
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM config_snapshots WHERE project_id=? AND digest=?",
                (snapshot.project_id, snapshot.digest),
            ).fetchone()
            if existing and self._snapshot(existing) != snapshot:
                raise ConfigError("immutable_snapshot_conflict")
            active = connection.execute(
                "SELECT s.* FROM config_snapshots s JOIN active_configs a "
                "ON s.project_id=a.project_id AND s.digest=a.digest WHERE a.project_id=?",
                (snapshot.project_id,),
            ).fetchone()
            if compare_active and (active["digest"] if active else None) != expected_digest:
                raise ConfigError("configuration_reload_conflict")
            if active:
                old = self._snapshot(active).config
                new = snapshot.config
                used_quota_policies = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT policy_id FROM quota_slots WHERE project_id=?",
                        (snapshot.project_id,),
                    )
                }
                old_quota_policies = {
                    name
                    for name, policy in old.operation_policies.items()
                    if policy.kind == "x_search"
                }
                new_quota_policies = {
                    name
                    for name, policy in new.operation_policies.items()
                    if policy.kind == "x_search"
                }
                rewired = [
                    profile.operation_policy_ref
                    for name, profile in old.aliases.items()
                    if profile.operation == "x_search"
                    and name in new.aliases
                    and new.aliases[name].operation_policy_ref != profile.operation_policy_ref
                ]
                if used_quota_policies and (
                    old_quota_policies != new_quota_policies
                    or not used_quota_policies.issubset(old_quota_policies)
                    or used_quota_policies.intersection(rewired)
                ):
                    raise ConfigError("quota_lineage_migration_required")
                if old_quota_policies != new_quota_policies or rewired:
                    if connection.execute(
                        "SELECT 1 FROM requests WHERE project_id=? "
                        "AND execution_status='in_progress' LIMIT 1",
                        (snapshot.project_id,),
                    ).fetchone():
                        raise ConfigError("quota_reload_requires_drain")
                if (
                    old.mode != new.mode
                    and connection.execute(
                        "SELECT 1 FROM requests WHERE project_id=? LIMIT 1", (snapshot.project_id,)
                    ).fetchone()
                ):
                    raise ConfigError("mode_change_requires_new_project_namespace")
                old_budgets = {
                    key: value.model_dump(mode="json") for key, value in old.budgets.items()
                }
                new_budgets = {
                    key: value.model_dump(mode="json") for key, value in new.budgets.items()
                }
                used_policies = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT policy_id FROM budget_buckets WHERE project_id=?",
                        (snapshot.project_id,),
                    ).fetchall()
                }
                if used_policies:
                    if set(old_budgets) != set(new_budgets):
                        raise ConfigError("budget_lineage_migration_required")
                    for alias_name, old_alias in old.aliases.items():
                        replacement_alias = new.aliases.get(alias_name)
                        previously_used = used_policies.intersection(old_alias.budget_policy_refs)
                        if replacement_alias and not previously_used.issubset(
                            replacement_alias.budget_policy_refs
                        ):
                            raise ConfigError("budget_lineage_migration_required")
                if old_budgets != new_budgets:
                    running = connection.execute(
                        "SELECT 1 FROM requests WHERE project_id=? "
                        "AND execution_status='in_progress' LIMIT 1",
                        (snapshot.project_id,),
                    ).fetchone()
                    if running:
                        raise ConfigError("budget_reload_requires_drain")
                    for key, policy in old.budgets.items():
                        used = connection.execute(
                            "SELECT 1 FROM budget_buckets WHERE project_id=? "
                            "AND policy_id=? LIMIT 1",
                            (snapshot.project_id, key),
                        ).fetchone()
                        replacement = new.budgets.get(key)
                        if used and (
                            replacement is None
                            or replacement.scope != policy.scope
                            or replacement.period != policy.period
                        ):
                            raise ConfigError("budget_lineage_migration_required")
                if old.timezone != new.timezone:
                    has_buckets = connection.execute(
                        "SELECT 1 FROM budget_buckets WHERE project_id=? UNION ALL "
                        "SELECT 1 FROM quota_slots WHERE project_id=? LIMIT 1",
                        (snapshot.project_id, snapshot.project_id),
                    ).fetchone()
                    if has_buckets:
                        raise ConfigError("timezone_lineage_migration_required")
            connection.execute(
                "INSERT OR IGNORE INTO config_snapshots "
                "(project_id,digest,payload,fixtures_payload,capability_revision) "
                "VALUES (?,?,?,?,?)",
                (
                    snapshot.project_id,
                    snapshot.digest,
                    snapshot.payload,
                    snapshot.fixtures_payload,
                    snapshot.capability_revision,
                ),
            )
            connection.execute(
                "INSERT INTO active_configs VALUES (?,?) ON CONFLICT(project_id) "
                "DO UPDATE SET digest=excluded.digest",
                (snapshot.project_id, snapshot.digest),
            )
            connection.execute(
                "INSERT INTO trace_outbox(project_id,event_json) VALUES (?,?)",
                (
                    snapshot.project_id,
                    canonical_json(
                        {
                            "event": "configuration_activated",
                            "config_digest": snapshot.digest,
                            "previous_digest": active["digest"] if active else None,
                        }
                    ),
                ),
            )
            for policy_id, policy in snapshot.config.budgets.items():
                connection.execute(
                    "UPDATE budget_buckets SET cap_usd=? WHERE project_id=? AND policy_id=?",
                    (str(policy.cap_usd), snapshot.project_id, policy_id),
                )

    def reload(self, path: Path, project_id: str) -> ConfigSnapshot:
        try:
            expected = self.get(project_id).digest
        except ConfigError:
            expected = None
        snapshot = load_project_config(path, expected_project_id=project_id)
        self.activate(snapshot, expected_digest=expected, compare_active=True)
        return snapshot
