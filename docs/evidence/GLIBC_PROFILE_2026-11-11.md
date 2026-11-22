# glibc advisory prerequisites in the restricted gateway profile

> Historical source analysis, not current artifact approval. Current behavior is
> specified in [the product specification](../SPECIFICATION.md) and [X support](../X_SEARCH.md).

Date: 2026-11-11. Candidate library: **2.41-12+deb13u4**; reviewed deployment
platforms: Linux ARM64/AMD64. The following are scoped call-path/applicability
inferences. They do not claim patched libc, safety of arbitrary native code, or
final image approval. The raw scanner remains closed pending T126.

| Primary advisory | Required facility/input | Gateway assessment |
| --- | --- | --- |
| [CVE-2010-4756](https://security-tracker.debian.org/tracker/CVE-2010-4756) | Attacker-controlled libc glob expressions | No libc-glob or shell expansion API; user content stays typed data. Python path selection uses owned paths and separately bounded descriptors. |
| [CVE-2018-20796](https://security-tracker.debian.org/tracker/CVE-2018-20796) | Crafted POSIX regexec pattern | Schema regex uses CPython re in a resource-limited child, not the libc POSIX engine. No POSIX-regex control is exposed. |
| [CVE-2019-1010022](https://security-tracker.debian.org/tracker/CVE-2019-1010022) | Existing stack memory corruption followed by stack-guard bypass | No independent HTTP exploit is identified by this finding. It is a secondary mitigation weakness, not evidence that arbitrary memory corruption is safe. Preserve this limitation; investigate any new reachable memory bug independently. |
| [CVE-2019-1010023](https://security-tracker.debian.org/tracker/CVE-2019-1010023) | Run ldd on attacker-supplied ELF | The API never executes ldd or supplied ELF; workers execute only the fixed installed Python entrypoint. No user-function/local-command execution. |
| [CVE-2019-1010024](https://security-tracker.debian.org/tracker/CVE-2019-1010024) | Thread/heap layout information used as an ASLR bypass | Secondary layout weakness without an independent attack path identified by this finding. It does not authorize memory-corruption bugs or a general ASLR-strength claim. |
| [CVE-2019-1010025](https://security-tracker.debian.org/tracker/CVE-2019-1010025) | Predictable pthread heap addresses used for another exploit | Same secondary prerequisite; there is no application interface exposing raw pointers or attacker ELF. Do not treat the vendor dispute alone as disposition. |
| [CVE-2019-9192](https://security-tracker.debian.org/tracker/CVE-2019-9192) | Crafted POSIX regexec pattern | Same separation from the libc regex engine; pathological Python patterns are killed by owned process limits. |
| [CVE-2026-18374](https://security-tracker.debian.org/tracker/CVE-2026-18374) | Attacker-controlled fopen mode with ccs extension | Mode strings belong to the authored runtime; requests supply content, not fopen modes/encodings. Secret/state access uses fixed os.open flags. |
| [CVE-2026-19499](https://security-tracker.debian.org/tracker/CVE-2026-19499) | Susceptible strfmon width formatting | USD uses exact Decimal/string formatting; no strfmon monetary formatting API or caller-controlled native format string. |
| [CVE-2026-19542](https://security-tracker.debian.org/tracker/CVE-2026-19542) | A deep tsearch/tdelete tree with extensive insertions/deletions | No native search-tree construction/deletion feature in the typed gateway path; reviewed native tokenizer usage is encode/decode, without custom FFI or training controls. This is a scoped call-path inference, not a claim that libc lacks the symbol. |
| [CVE-2026-5435](https://security-tracker.debian.org/tracker/CVE-2026-5435) | Deprecated DNS record pretty-printer processing TSIG | Owned DNS uses getaddrinfo, without the debug record-printer interface. The vulnerable printer is distinct from normal resolver execution. |
| [CVE-2026-6238](https://security-tracker.debian.org/tracker/CVE-2026-6238) | Deprecated DNS debug-printer parsing special records | No such debug-printer call; normal getaddrinfo resolution is not this facility. |
| [CVE-2026-6368](https://security-tracker.debian.org/tracker/CVE-2026-6368) | wordexp append and subsequent wordfree | No shell/wordexp evaluation API; owned Python subprocess arguments are explicit lists. |
| [CVE-2026-6791](https://security-tracker.debian.org/tracker/CVE-2026-6791) | Unbounded username expansion in wordexp | No user text enters wordexp; no local shell/function execution. |
| [CVE-2026-77117](https://security-tracker.debian.org/tracker/CVE-2026-77117) | SHIFT_JISX0213 iconv with a split output character | Protocol/config/fixtures use explicit UTF-8. No alternate iconv conversion API or provider-selected charset decoder is exposed. |
| [CVE-2026-80489](https://security-tracker.debian.org/tracker/CVE-2026-80489) | EUC_JISX0213 iconv with a split output character | Same UTF-8-only boundary; no such native charset conversion path. |
| [CVE-2026-84243](https://security-tracker.debian.org/tracker/CVE-2026-84243) | Untrusted LANGUAGE/catalog selection in a non-C locale | Workers use C.UTF-8 and fixed internal credential variables. Project source names cannot inject LANGUAGE/LC controls. Root-owned installed assets and operator-owned parent environment remain trusted prerequisites. |
| [CVE-2026-8674](https://security-tracker.debian.org/tracker/CVE-2026-8674) | Oversized resolver search configuration or LOCALDOMAIN | The owned resolver checks a bounded root-owned regular resolv.conf, rejects oversized search/domain lines and LOCALDOMAIN before lookup. Compose supplies a short search domain; credential names are rebound before startup. This is an explicit input mitigation, not a libc patch. |
| [CVE-2026-86805](https://security-tracker.debian.org/tracker/CVE-2026-86805) | Privileged AT_SECURE ELF with ORIGIN traversal plus hardlink/symlink race | Runtime executes fixed non-setuid Python as UID 10001, with dropped caps/no-new-privileges/read-only root in the required profile. No supplied ELF or privileged mount/command path. |
| [CVE-2026-89092](https://security-tracker.debian.org/tracker/CVE-2026-89092) | nscd enabled against a malicious DNS response | No nscd daemon is launched by the gateway image/Compose entrypoint. DNS goes through the owned resolver/proxy path; host daemons are a separate host trust boundary. |
| [CVE-2026-95818](https://security-tracker.debian.org/tracker/CVE-2026-95818) | ORIGIN processing while loading setuid/setgid AT_SECURE ELF | No such privileged ELF execution; the fixed Python worker is non-setuid and the deployment prevents acquiring new privileges. |
| [CVE-2026-97399](https://security-tracker.debian.org/tracker/CVE-2026-97399) | Power8-specific strncasecmp implementation | Reviewed runtime platforms are ARM64 and AMD64, not Power8. A new architecture requires a fresh artifact/path review. |

These conclusions require fixed typed operations, no custom SDK/callback/FFI/
training controls, no local user-function execution, fixed owned Python entrypoints,
private state and trusted installed assets. Child credential handoff now removes
the ability to reuse original environment names as OS startup controls. Parent
administrator/environment, host/root compromise and arbitrary owner-provided
native programs are outside this profile; they are not made safe by a sandbox
claim. The Python child socket guard supplements the deployment firewall and is
not an OS sandbox against arbitrary native code.

The three disputed stack/layout mitigation findings are not treated as patched
or dismissed merely because disputed. Their secondary exploit prerequisite must
be revisited if any reachable memory-corruption finding appears. They do not
provide an independent remote attack through the current typed interface. This
limitation stays visible in any final disposition.

Evidence includes source inspection of fixed process/transport/schema paths,
actual nonroot/read-only/no-new-privileges container checks, two independent
network stacks, resolver search rejection, real child credential rebinding,
resource-limited Python regex tests and the exact native source/platform records.
The existing source/inlined-code limitations still apply. Final artifact/source
fingerprints, all advisory matches and architecture-specific checks remain required.
