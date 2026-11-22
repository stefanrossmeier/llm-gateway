# Design evolution and maintenance

The gateway began by identifying the common contracts behind several independent
applications, then separating reusable transport/accounting from consumer-owned
model policy. This public design record preserves those decisions without private
application source links or an installation inventory.

1. Define immutable typed contracts and a lean client before introducing provider SDKs.
2. Establish exact-money reservations, durable dispatch intent and independent
   execution/output/billing states. Treat transport loss as uncertainty, not free work.
3. Add owned provider processes, key-free validation and explicit credential/network boundaries.
4. Translate compatibility wires into the canonical pipeline rather than fork accounting.
5. Add scoped receipts, reconciliation, result retention, quotas and fenced backup/restore.
6. Verify offline fault cases, real SQLite races, mocked SDK HTTP and restricted containers.
7. Distinguish dated advisory applicability from patched-component or generally clean-scan claims.
8. Replace process-scoped invocation nonces with durable job identity in consumers.
9. Expose cost reporting with calendar windows, grouping and explicit uncertainty counts.
10. Correct native X admission: per-item charges require an explicit risk allowance;
    tool counts and returned results do not provide an upstream monetary guarantee.

Future work should preserve these invariants. New providers or SDK versions need
reviewed transport/capability/price behavior; new state versions need migration
proof. Paid provider acceptance is recorded by exact scope, never generalized
from fake or mocked execution. Public releases retain their exact immutable evidence.
