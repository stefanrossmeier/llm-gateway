# Publication review

Public source must contain no credentials, real prompts, private state, deployment
inventory or private consumer configuration. Specs and technical source reviews
may remain when they describe the reusable product without private project data.

`python3 scripts/check_publication.py` scans tracked source and reachable history
for limited secret patterns, checks private-state paths and required documents,
and never prints matched secret values. Run an independent redacted secret scan
as well. Pattern checks are evidence, not proof that all private facts are absent.

The public release uses a reviewed source snapshot. Private development history
and superseded private artifacts are retained separately, not exposed merely to
preserve chronology. Existing specs are maintained as current product design;
[design evolution](IMPLEMENTATION_PLAN.md) explains the major decisions.

Before visibility changes, verify the complete reachable refs and release assets,
check that only intended public material is present, and establish a usable
private vulnerability reporting route. Public docs describe generic supported
installation; they do not disclose application deployments or their locations.
