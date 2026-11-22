# Consumed cost and exposure

Authenticate to `GET /v1/usage` using a project token. `usage:project` grants
project-wide reporting; `receipts:own` restricts the report to that principal.
Fixed run/phase grants also restrict reports. Requests cannot select another
project, and scoped reports never expose shared bucket balances.

```text
GET /v1/usage
GET /v1/usage?group_by=alias
GET /v1/usage?group_by=day&since=2026-11-01T00:00:00Z&until=2026-12-01T00:00:00Z
GET /v1/usage?group_by=run_id
GET /v1/usage?alias=example-chat&group_by=actual_model
```

`since` is inclusive and `until` exclusive; both require timezone-aware ISO-8601
instants. Day/month grouping uses the project's configured timezone. Reports
select attempts by their invocation's creation time, including late settlement.
Unsupported filters and invalid dates fail rather than silently widening scope.

| Field | Meaning |
| --- | --- |
| `measured_usd` | Known provider-reported cost subtotal; unknown charges are excluded, not zeroed |
| `estimated_usd` | Separate estimate subtotal, never additional measured spend |
| `protected_debit_usd` | Total protected ledger debit, including holds/reservations |
| `contingent_exposure_usd` | Protected exposure beyond known measured/estimated amounts |
| `unknown_attempts` | Attempts without complete billing evidence |
| `measured_attempts` / `estimated_attempts` | Counts by billing evidence type |
| `unsettled_attempts` | Attempts still reserved or dispatched |
| `usage_complete` | Whether selected billing evidence is complete |
| `overrun` | Whether any selected charge exceeded its original reservation |
| `buckets` | Unfiltered project-authorized budget balances; overlapping policies, not additive spend |

Amounts are exact decimal USD strings. An empty report has zero known spending;
a report with unknown attempts does not establish zero actual spending. Inspect
receipts for request, provider, model, measured/estimated cost and reservation
kind. X's `risk_allowance` must not be described as an enforced upstream cap.
Historical X receipts without an explicit classification are
`legacy_unclassified`, never retroactively promoted to enforced bounds.

A provider failure or invalid output can still cost money. Persist attempt
receipts, keep unresolved exposure, and reconcile using verified provider evidence.
Never delete or reset a ledger to regain allowance.
