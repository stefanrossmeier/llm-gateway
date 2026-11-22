# Contributing

Use issues for non-sensitive reproducible bugs and focused pull requests. Report
vulnerabilities through [SECURITY.md](SECURITY.md). Follow the [code of conduct](CODE_OF_CONDUCT.md).

Use Python 3.12 and uv. Run the commands in [testing](docs/TESTING.md). Reinstall
the authored package after source changes because isolated `python -I` workers
load the installed package. Add meaningful regressions for changed money,
authority, recovery, schema and isolation behavior; keep tests inference-free by default.

Keep client/contracts free of service/provider SDK imports. Preserve project-owned
policy and explicit transport boundaries. Never commit credentials, real prompts,
private application/infrastructure details or accounting state. Review new dependencies
statically before installation. Explain behavior, validation and material limitations
in each change. Public documentation describes the reusable product, not deployments.
