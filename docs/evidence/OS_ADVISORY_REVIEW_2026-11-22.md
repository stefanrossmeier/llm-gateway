# Additional OS advisory source review — 2026-11-22

These are restricted-path applicability conclusions, not patched-component claims.
The exact release assessment must bind this document, the authored source,
reviewed dependency wheels, container filesystem and actual native profiles.

| Advisory | Required vulnerable operation | Reviewed gateway restriction |
| --- | --- | --- |
| CVE-2026-91094 | dpkg-source extraction of an attacker-controlled quilt source package with malicious symlinks and failed patch restoration | The runtime exposes no package/archive extraction or shell execution. Builds install reviewed wheels and fixed signed-index/hash-checked binary packages, not user-supplied source packages. |
| CVE-2026-115819 | Wide scanf conversions into a caller-owned narrow buffer using `%s`/`%[` | Python JSON/schema/text validation does not use this interface. Native wheel and CPython extension imports are recorded by the probe; affected wide-scanf imports are rejected. The API exposes no arbitrary ctypes/FFI, native format strings or custom extension execution. |
| CVE-2026-100290 | libc POSIX regcomp/regexec allocation-failure handling | Schemas use resource-limited CPython regular expressions. Gateway native extension imports of POSIX regex functions are rejected by the probe; arbitrary FFI and user-selected native regex engines are not supported. |
| CVE-2026-125709 | libstdc++ file-stream buffering after repeated failed single-byte output | Inference routes do not expose native file-stream writes or tokenizer training. The reviewed C++/Rust dependency source paths and exact native imports are constrained as described in the C++ source assessment. SQLite/Python file operations are not C++ stream output. New native libraries or training/export features require a fresh review. |

Primary sources: [dpkg](https://security-tracker.debian.org/tracker/CVE-2026-91094),
[wide scanf](https://security-tracker.debian.org/tracker/CVE-2026-115819),
[POSIX regex](https://security-tracker.debian.org/tracker/CVE-2026-100290),
[C++ streams](https://security-tracker.debian.org/tracker/CVE-2026-125709).

The glibc and C++ findings have no fixed vendor version recorded at review.
This review therefore relies on specific unreachable operations and enforced
runtime restrictions. It does not authorize arbitrary native programs, plugins,
shell tools, owner-selected SDK hooks or untrusted build artifacts. The bytecode/
source and ELF import evidence is complementary; dynamic symbols alone cannot
prove absence of vulnerable inlined source. The existing dated C++ source review
remains necessary. All prior source-assessment limitations remain in force.

Require UID 10001, read-only root, no capabilities, no-new-privileges, private
state and allowlisted isolated egress. An image used with a weaker profile is
outside this assessment. Raw advisory reports are retained without suppression.

## Additional findings in the fresh exact-image scan

The current database adds the following findings beyond the earlier source
review. These dispositions apply only with the same typed API and fixed
transport, interpreter, dependency and runtime restrictions.

| Primary advisory | Vulnerable operation | Scoped source/path conclusion |
| --- | --- | --- |
| [CVE-2026-121757](https://security-tracker.debian.org/tracker/CVE-2026-121757) | glibc CP949 iconv with //IGNORE | Protocol/configuration use explicit Python Unicode/JSON/UTF-8 handling. No request can choose a native iconv conversion or //IGNORE target. |
| [CVE-2026-121758](https://security-tracker.debian.org/tracker/CVE-2026-121758) | glibc ISO-2022-CN-EXT iconv | No native iconv conversion surface is exposed; provider charset headers do not choose a glibc codec for JSON parsing. |
| [CVE-2026-124024](https://security-tracker.debian.org/tracker/CVE-2026-124024) | glibc UCS-4 to IBM1390/1399 iconv | No such output encoding or native conversion API is reachable through typed inference or state operations. |
| [CVE-2026-126765](https://security-tracker.debian.org/tracker/CVE-2026-126765) | libc glob allocation-failure cleanup | No libc glob/word expansion or user-controlled filesystem pattern API; owned paths and explicit subprocess argument lists are used. |
| [CVE-2026-124709](https://security-tracker.debian.org/tracker/CVE-2026-124709) | OpenSSL QUIC server rejected-stream handling | The service has no QUIC or UDP endpoint. Provider connections are ordinary HTTPS/TCP clients; the CONNECT proxy is opaque TCP. |
| [CVE-2026-95690](https://security-tracker.debian.org/tracker/CVE-2026-95690) | OpenSSL DTLS fragment reassembly | No DTLS socket/context is created. HTTPS/TCP does not reach the DTLS path. |
| [CVE-2026-121482](https://security-tracker.debian.org/tracker/CVE-2026-121482) | CMS/PKCS#7 verification with CRL checking | The API offers no CMS/PKCS#7 verification, signing or untrusted document/certificate import facility. |
| [CVE-2026-121481](https://security-tracker.debian.org/tracker/CVE-2026-121481) | EVP_EncodeBlock near-INT_MAX input | Requests/results are bounded to at most 16 MiB; no arbitrary EVP encoding or attacker-selected native buffer length is exposed. Python base64 encoding is not this OpenSSL API. |
| [CVE-2026-84786](https://security-tracker.debian.org/tracker/CVE-2026-84786) | OpenSSL DANE/TLSA validation | The owned HTTP client uses standard CPython SSLContext HTTPS verification; DANE/TLSA APIs and custom validation hooks are not enabled. |
| [CVE-2026-35190](https://security-tracker.debian.org/tracker/CVE-2026-35190) | TLS server context with Suite B certificate flags | No OpenSSL TLS server or Suite B certificate-flag API exists in the service. Client TLS configuration is fixed by the owned transport. |
| [CVE-2026-107493](https://security-tracker.debian.org/tracker/CVE-2026-107493) | PEM_X509_INFO_read with an existing caller stack | The gateway exposes no PEM-stack API or request-controlled CA file. Standard OpenSSL CA-loading callers pass NULL for that stack; installed trust files are reviewed read-only inputs. |
| [CVE-2026-115559](https://github.com/python/cpython/issues/160369) | imaplib STARTTLS read-ahead buffer | No IMAP client or STARTTLS operation is exposed; egress permits only the declared HTTPS providers on TCP 443. |
| [CVE-2026-115791](https://github.com/python/cpython/issues/161705) | gzip/tarfile/zipfile CLI extraction into CWD | The runtime invokes fixed Python entrypoints with -I and provides no archive extraction/CLI execution surface. Review/build tooling reads hash-verified wheels rather than extracting attacker archives into a CLI working directory. |
| [CVE-2026-124249](https://github.com/python/cpython/issues/161569) | Cross-domain deletion from a shared CookieJar | Provider invocation creates an owned HTTP client for its selected fixed host. No cross-domain persistent cookie authentication is used; bearer authority is explicit and provider response cookies are not forwarded to consumers. |
| [CVE-2026-16052](https://github.com/python/cpython/issues/161707) | Untrusted Morsel attribute output | The API does not emit user-selected cookies or use cookie/session authentication. Authentication is bearer/verified IPC, and typed JSON output is independent of Set-Cookie attributes. |
| [CVE-2026-19552](https://github.com/python/cpython/issues/161503) | ssl.get_server_certificate identity omission | That helper is not called. HTTPS uses the owned HTTPX client and normal verified SSLContext hostname validation. |

The reviewed call paths are `providers/runtime.py` (fresh owned HTTP client,
fixed provider hosts and complete response capture), `service/api.py` and
`service/server.py` (typed JSON/bearer HTTP), `isolation/process.py` (fixed -I
children and scrubbed environment), `service/validation_worker.py` (bounded
key-free Python schema/regex), `service/egress_proxy.py` (allowlisted TCP CONNECT),
and `storage/backup.py` (SQLite backup, not archive extraction). None exposes
arbitrary FFI, native codec selection, IMAP, QUIC/DTLS, CMS verification, cookie
attributes or caller-supplied CA loaders. The same restrictions apply when an
upstream response is malformed; billing is preserved independently of parsing.

The fresh native probes on both architectures report no sensitive native
wide-scanf/POSIX-regex imports, and the reviewed C++ objects import allocation,
exception and ios initialization symbols rather than the vulnerable file-output
stream operation. The C++ source assessment remains necessary for inlined code.
These are specific applicability inferences with stated prerequisites, not a
claim that dynamic import inspection proves arbitrary native code safe.

All raw matches, component versions and vulnerability namespaces remain in the
release reports. A changed profile, newly reachable feature, new advisory or
expired assessment requires review again; CI does not renew approvals itself.
