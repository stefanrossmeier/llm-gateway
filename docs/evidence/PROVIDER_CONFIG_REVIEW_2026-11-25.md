# Provider configuration and exact-image review — 2026-11-25

The provider changes keep endpoints in trusted project configuration. Callers
still select authorized model aliases; requests cannot inject URLs, credentials,
SDK hooks or arbitrary protocol parameters. Definition reads are bounded, explicit,
relative to the project root, and use no-follow directory-relative opens. Complete
resolved definitions are captured in immutable snapshots. The two bundled provider
files participate in authored-source identity checks.

The generic adapter uses one owned HTTPX POST with verified HTTPS, exact URL,
authentication, model, parameter and byte checks. It disables redirects, environment
proxies, implicit retries and response compression. Anonymous access sends no
Authorization value. Provider children retain selected environments and the same
network/process restrictions; direct DNS and the egress proxy reject private or
mixed answers. Egress uses enabled provider destinations from project configuration
without provider credential mounts. No untrusted code, FFI, archive extraction,
new native codec selection or new TLS options are exposed by these changes.

Existing C++/glibc/OpenSSL/Python/SQLite source reviews remain prerequisites. Their
unreachable-operation conclusions were rechecked against these paths. The profile
requires UID 10001, no capabilities, no-new-privileges, a read-only root and isolated
allowlisted egress. Raw findings remain in the release; this is not a clean-OS claim.

## Newly reported OS findings

The fresh scans included the following findings beyond the prior assessment.

| Advisory | Reviewed prerequisite and release action |
| --- | --- |
| [CVE-2026-107494](https://openssl-library.org/news/secadv/20261124.txt) | OpenSSL QUIC frame queues. Gateway HTTPX/CPython TLS uses TCP; no QUIC/UDP API or custom TLS hooks are enabled. The CONNECT proxy is opaque TCP. Not reachable under this profile. |
| [CVE-2026-84785](https://security-tracker.debian.org/tracker/CVE-2026-84785) | Incoming QUIC stream fragment reassembly. The gateway creates no QUIC connections or stream-acceptance policy. Not reachable under this profile. |
| [CVE-2026-135901](https://github.com/shadow-maint/shadow/commit/1112ed7e741d4db15272d0fee910a5c3360c3457) | Shadow newgidmap/newuidmap descriptor/path handling. The exact runtime lacks `/usr/bin/newgidmap`; it exposes no user-namespace/identity-map operations or caller-selected commands and runs without capabilities or privilege escalation. The scanner associates the advisory with installed shadow source-package components. Not reachable under this profile. |
| [CVE-2026-124851](https://security-tracker.debian.org/tracker/CVE-2026-124851) | Zstandard decompression can disclose stale buffer contents. The fixed Debian `libzstd1` version `1.5.7+dfsg-1+deb13u1` is installed in both architectures. The [upstream patch](https://github.com/meta/zstd/commit/0c9d297531a9f15a717ff24604afc0843b3b1427) and signed-repository binary package controls were reviewed before installation. |

Zstandard is loaded indirectly during HTTPX/SSL imports even though HTTPX exposes
only identity/gzip/deflate decoders in this image. An assumption that the library
was absent would have been incorrect. The available fixed package was selected
instead of relying on that assumption. Both reviewed packages contain only
control/md5sums/shlibs and the existing `activate-noawait ldconfig` trigger, with
no preinst/postinst/prerm/postrm/config scripts. Exact pins and platform SHA-256
values are enforced in `deploy/install_security_updates.sh`.

Free Kilo inference was exercised with a public sample prompt and recovered by
the same request ID. It does not establish paid-provider or universal model
acceptance. No paid inference or Telegram delivery is part of release validation.
The exact runtime profiles, raw scans, source hashes and expiring assessment are
required together; new findings or altered bytes require another review.
