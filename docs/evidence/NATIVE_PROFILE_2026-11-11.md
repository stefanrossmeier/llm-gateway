# Native runtime evidence and remaining C++ review

> Historical source analysis, not current artifact approval. Current behavior is
> specified in [the product specification](../SPECIFICATION.md) and [X support](../X_SEARCH.md).

Date: 2026-11-11. Exact inspected Linux ARM64 candidate:
`sha256:d1963152d3bee43e815e1d01587fc27bd9cf351b0a67ddfca6bc9398a557b9e8`.
This is the package-26 image, not an assertion about a newly rebuilt later image.
The [structured probe](native-profile-arm64-2026-11-11.json) records its identity
and the exact authored adapter-fixture hash. **Applicability approval is false.**

Reproduce with Docker and the checked-out authored fixture (no pytest install
is needed inside the image):

```sh
python3 scripts/probe_native_profile.py --image REVIEWED_LOCAL_IMAGE \
  --output /absolute/private/new-native-profile.json
```

The probe disables pulls and container networking, uses a read-only unprivileged
container and synthetic credentials, exercises all eight actual pinned SDK
provider/operation combinations over mocked HTTP, and records the image actually
used through its task-owned container. It removes only that probe container.
It performs no inference, reads no real keys and never creates an approval.

## Observed library use

The process **loads libstdc++.so.6.0.33**. Two installed ELF objects reference
libstdc++: frozenlist's ARM64 extension and `tokenizers/tokenizers.abi3.so`.
Frozenlist imports no named C++ symbols; tokenizers imports ten allocation,
exception/type-information and iostream-initialization symbols. Their exact
mangled names are retained in the JSON evidence.

This rules out claiming that the C++ library is simply unused. Absence of an
undefined dynamic symbol is not proof that an affected C++ template was never
compiled inline into a wheel. Merely changing an installed runtime library can
also be insufficient for an affected header implementation compiled into a wheel.

## Primary findings still requiring disposition

| Finding | Facility identified by the primary Debian advisory | Review limitation |
| --- | --- | --- |
| [CVE-2026-95619](https://security-tracker.debian.org/tracker/CVE-2026-95619) | Aligned allocation overflow | The observed imports use ordinary allocation; prove actual call reachability or use reviewed fixed artifacts rather than treating that observation alone as approval. |
| [CVE-2026-102010](https://security-tracker.debian.org/tracker/CVE-2026-102010) | Priority-queue erase-if use-after-free | Requires wheel/header/call-path review; loaded-library metadata alone cannot resolve it. |
| [CVE-2026-114690](https://security-tracker.debian.org/tracker/CVE-2026-114690) | Parallel search buffer bounds | Check compiled feature use and corresponding fixed artifacts. |
| [CVE-2026-114691](https://security-tracker.debian.org/tracker/CVE-2026-114691) | Oversized boolean-vector construction | Request size bounds help constrain data but are not a substitute for identifying the affected native implementation. |
| [CVE-2026-115455](https://security-tracker.debian.org/tracker/CVE-2026-115455) | Wide-character bitset extraction | Establish absence/reachability of the compiled feature before a profile disposition. |
| [CVE-2026-115456](https://security-tracker.debian.org/tracker/CVE-2026-115456) | Numeric conversion over-read | Need native call-path/source evidence, not only dynamic import names. |

These are pending review questions, not claims that the gateway exposes an
exploit. The exact raw package matches remain visible and the security gate
still rejects them. No blanket severity, wont-fix or unused-library suppression
was added. Other container findings and the separately documented OpenSSL/DNS
profile reviews remain independent. Repeat on final artifacts and other supported
architectures; ARM64 evidence cannot establish AMD64 native equivalence.

The subsequent [exact source-path assessment](CPP_SOURCE_PROFILE_2026-11-11.md)
examines the advisory prerequisites using signed source commits and the locked
crate checksum. Final artifact/platform disposition and T126 are still required.
