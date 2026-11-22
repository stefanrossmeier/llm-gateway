# Release acceptance

- [ ] Exact source/version/lock and generated schemas agree.
- [ ] Lint, formatting, complete offline suite and every-module branch gate pass.
- [ ] Lean client imports pass on Python 3.11 and 3.12.
- [ ] Fake quickstart, container restrictions, lifecycle and native mocked adapters pass.
- [ ] Exact image SBOMs/scans and current scoped advisory assessments are verified.
- [ ] Secret/history/artifact disclosure review passes.
- [ ] Release manifests, hashes and published artifact identity verify.
- [ ] Private vulnerability reporting is enabled and reachable.

Live acceptance must state its actual provider/model/operation, test scope and
measured/unknown cost. Offline verification does not imply all paid features were
executed. No live-test approval is created by checking this document.
