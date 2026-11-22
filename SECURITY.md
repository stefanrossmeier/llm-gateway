# Security policy

Report vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/stefanrossmeier/llm-gateway/security/advisories/new).
Do not put credentials, real prompts, private configuration, exploitable details
or ledger dumps in public issues. If the route is unavailable, contact the
maintainer through their GitHub profile to arrange a private channel before
sharing sensitive details.

Only the current reviewed alpha is maintained; there is no stable/LTS promise.
Security fixes identify affected versions and reviewed replacements in release
notes. Arbitrary SDK/dependency versions are not supported.

Use authenticated HTTP with TLS across untrusted networks, or verified private
Unix IPC. Isolate project state/secrets. Use non-root/read-only containers,
dropped capabilities, no-new-privileges, finite resources and restricted egress.
The socket guard is not a general native-code sandbox.

Dated dependency/native/OS assessments apply only to their exact artifacts and
restricted profile. Raw scanner matches are retained; a scoped disposition does
not mean a vulnerable package was patched. New findings or changed/expired evidence
fail release review. Native X accepts explicitly configured financial exposure;
its allowance is not an enforced upstream spending cap.
