# Release procedure

1. Review dependencies statically before installing changed artifacts. Keep the
   exact LiteLLM pin and frozen wheel graph; review native/platform behavior.
2. Reinstall the authored package, run offline/lean-client/coverage checks, verify
   schemas, execute the fake quickstart and build the wheel.
3. Build the exact Linux artifacts, produce SBOMs/raw scans, run fake/native/runtime
   restrictions checks, and review every advisory against those bytes and sources.
4. Record dated, expiring applicability evidence. Unreviewed findings, altered
   source/image/profile or expired assessments fail. Never suppress raw matches
   or call a restricted-profile disposition a generally clean scan.
5. Run publication checks over source/history/artifacts. Exclude secrets, private
   application details, infrastructure inventory and private development records.
6. Create a versioned immutable release with artifact hashes, source identity,
   dependency graph and provenance. Never replace existing immutable assets.
7. Verify the published bytes and private security-reporting route. Consumers pin
   that reviewed release identity and preserve their own state/configuration.

The security workflow collects candidate evidence for source, build, dependency,
deployment and security changes. Successful collection reports manual review
required; it does not grant release approval. The exact released-asset job is
the strict promotion/monitoring gate and rejects any unreviewed finding. Scheduled monitoring scans exact released image
archives, not a newly rebuilt substitute. It cannot deploy, spend on inference,
renew its own assessment or grant publication approval.

State schema migrations are append-only. Preserve released historical DDL,
prove supported migrations retain money/history/quota, and require a fenced
restore when external billing gaps may exist. See [operations](OPERATIONS.md).
