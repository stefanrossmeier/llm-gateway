# Python advisory prerequisites in the gateway profile

> Historical source analysis, not current artifact approval. Current behavior is
> specified in [the product specification](../SPECIFICATION.md) and [X support](../X_SEARCH.md).

Date: 2026-11-11. Candidate interpreter: CPython **3.12.16**. This records
source/profile applicability inferences for the eight raw Python findings. It is
not a patched-interpreter claim, a general Python security assessment or final
container approval. The raw scanner continues to fail until T126 is resolved.

| Primary advisory | Required facility/input | Restricted gateway assessment |
| --- | --- | --- |
| [CVE-2025-12781](https://security-tracker.debian.org/tracker/CVE-2025-12781) | Alternate-alphabet base64 decoding | Embeddings use strict standard-alphabet decoding. Gateway bearers are compared by digest, without JWT or URL-safe decode. Token generation encodes only. No alternate-alphabet decoder is exposed by the typed provider path. |
| [CVE-2026-3446](https://security-tracker.debian.org/tracker/CVE-2026-3446) | Lenient decoding of data after padded base64 | The embedding decoder explicitly uses `validate=True`; regressions reject appended data, malformed padding and byte/dimension mismatches. |
| [CVE-2025-15366](https://security-tracker.debian.org/tracker/CVE-2025-15366) | Untrusted IMAP commands | No IMAP transport or command API in the owned gateway path. |
| [CVE-2025-15367](https://security-tracker.debian.org/tracker/CVE-2025-15367) | Untrusted POP commands | No POP transport or command API in the owned gateway path. |
| [CVE-2026-103995](https://security-tracker.debian.org/tracker/CVE-2026-103995) | `http.client` response chunk parsing | Provider traffic uses the owned HTTPX transport; inbound Uvicorn uses its locked HTTP implementation. No caller chooses a different SDK/transport or invokes this parser. |
| [CVE-2026-116566](https://security-tracker.debian.org/tracker/CVE-2026-116566) | NUL in getaddrinfo host/port | Provider origins are constants. The explicit proxy rejects original control/whitespace/DEL before URL normalization; NUL cannot reach owned resolution. See package 44. |
| [CVE-2026-3479](https://security-tracker.debian.org/tracker/CVE-2026-3479) | Untrusted resource path passed to pkgutil | Package resource selection belongs to the installed pinned code. No resource-path/custom SDK/plugin input is exposed; fixtures use separately bounded directory-relative descriptors. The assessment does not rely only on the disputed classification. |
| [CVE-2026-6019](https://security-tracker.debian.org/tracker/CVE-2026-6019) | HTML script generation with attacker-controlled cookies | The service returns typed JSON, without `Morsel.js_output` or an HTML/cookie-generation surface. |

These conclusions apply to fixed OpenAI/OpenRouter typed operations, disabled
callbacks/telemetry, no custom SDK/resource/transport controls, and the supplied
scrubbed worker/deployment profile. They do not approve arbitrary Python, LiteLLM,
OAuth/mail/plugin execution or user-provided Python code. New call paths or
runtime artifacts require review. Dynamic imports alone are not proof that every
stdlib feature is absent.

Evidence: `service/output.py`, `service/auth.py`, `providers/runtime.py`,
`providers/process.py`, `isolation/network.py`, and the actual adapter, strict
base64, proxy, credential-handoff and API-control regressions. This is an authored
summary of the linked primary advisory prerequisites, without copying their
full descriptions. Final actual-image/SBOM matching and the other findings
remain separate gates.
