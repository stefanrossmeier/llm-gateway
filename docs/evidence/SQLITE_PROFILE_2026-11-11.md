# SQLite advisory prerequisites in the private ledger profile

> Historical source analysis, not current artifact approval. Current behavior is
> specified in [the product specification](../SPECIFICATION.md) and [X support](../X_SEARCH.md).

Date: 2026-11-11. Candidate library: **3.46.1-7+deb13u2**. The fourteen raw
findings below require SQL, changesets, extensions or database/WAL input outside
the supported private-ledger trust boundary. These are scoped source inferences,
not patched-library claims or final container approval. T126 remains open.

The service constructs fixed parameterized SQL. Public report dimensions are
allowlisted and selected in Python; callers cannot supply SQL, schema objects,
SQLite extensions, changesets, a database file or WAL bytes. Private owner-only
local state and ancestors exclude another OS user as a writer. The service's
original v1/v2/v3 tables/indexes/constraints are recognized before migration;
FTS/R-Tree/session/zipfile objects are not part of that schema. None is enabled
by an inference/tool result. Returned functions are never executed.

| Primary advisory | Required facility/input | Scoped assessment |
| --- | --- | --- |
| [CVE-2021-45346](https://security-tracker.debian.org/tracker/CVE-2021-45346) | Corrupted database record | No untrusted database import or other-user writer; private owned state only. |
| [CVE-2025-70873](https://security-tracker.debian.org/tracker/CVE-2025-70873) | zipfile extension and crafted ZIP | No extension loading or SQL archive interface. |
| [CVE-2026-119371](https://security-tracker.debian.org/tracker/CVE-2026-119371) | Huge chained CTE SQL/view | No arbitrary SQL or view in recognized schema. |
| [CVE-2026-119372](https://security-tracker.debian.org/tracker/CVE-2026-119372) | FTS3/4 MATCH and snippet | No FTS tables, MATCH or snippet path. |
| [CVE-2026-119373](https://security-tracker.debian.org/tracker/CVE-2026-119373) | Crafted FTS5 detail-none prefix lists | No FTS5 tables or prefix-query path. |
| [CVE-2026-119374](https://security-tracker.debian.org/tracker/CVE-2026-119374) | Corrupted FTS5 index and secure delete | No FTS5 index or untrusted index input. |
| [CVE-2026-119375](https://security-tracker.debian.org/tracker/CVE-2026-119375) | Crafted database/WAL page sizes | The gateway creates its own WAL; no API accepts replacement database/WAL bytes. |
| [CVE-2026-119376](https://security-tracker.debian.org/tracker/CVE-2026-119376) | Very deep chained COLLATE SQL/view | No arbitrary SQL or views; user text is bound data. |
| [CVE-2026-119377](https://security-tracker.debian.org/tracker/CVE-2026-119377) | SQL RIGHT/FULL USING joins under SQLite authorizer policy | Access control is enforced by authenticated project/principal selection and fixed SQL, without a user-SQL/authorizer sandbox. |
| [CVE-2026-119378](https://security-tracker.debian.org/tracker/CVE-2026-119378) | Altered R-Tree shadow schema/triggers | No R-Tree or mutable caller schema. |
| [CVE-2026-119379](https://security-tracker.debian.org/tracker/CVE-2026-119379) | Crafted session changeset application | No session or changeset-apply interface. |
| [CVE-2026-119591](https://security-tracker.debian.org/tracker/CVE-2026-119591) | Large VALUES/view expression | No arbitrary SQL/views; fixed queries are bounded authored code. |
| [CVE-2026-50812](https://security-tracker.debian.org/tracker/CVE-2026-50812) | Malformed session changeset blob | No session changeset application. |
| [CVE-2026-50813](https://security-tracker.debian.org/tracker/CVE-2026-50813) | Session changeset concatenate/changegroup merge | No such session API or supplied changeset. |

Backup/restore is an owner CLI workflow for the gateway's own trusted backups,
not a safe database parser for arbitrary files. Verify a source digest against a
trusted recorded checkpoint. Owner-only mode, recognized schema, quick-check and
foreign-key checks are integrity controls; **they do not prove an adversarial
SQLite file is safe to parse**. Do not restore third-party databases. An operator
who replaces the private database/WAL or runs arbitrary SQLite extensions leaves
this profile; schema verification alone does not neutralize parser exploits.

Behavioral evidence includes actual SQL races, parameterized admission/reporting,
rejected schema/constraint tampering, foreign-key checks, private filesystem
boundaries and interrupted-restore fencing. A future SQL extension, import API,
network/shared state or new library artifact invalidates these conclusions.
Final exact-image/SBOM matching and other findings remain separate gates.
