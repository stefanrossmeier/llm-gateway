# OS utility/service advisory prerequisites

> Historical source analysis, not current artifact approval. Current behavior is
> specified in [the product specification](../SPECIFICATION.md) and [X support](../X_SEARCH.md).

Date: 2026-11-11. This covers 38 distinct utility/service findings in the pinned
Debian 13 candidate. It distinguishes source-package matches from use of the
specific vulnerable executable/function. It does not claim the packages are
patched/absent or allow arbitrary programs inside the image. The raw advisory gate
is unchanged pending final T126 artifact/profile matching.

| Primary advisory | Required facility | Restricted gateway inference |
| --- | --- | --- |
| [CVE-2005-2541](https://security-tracker.debian.org/tracker/CVE-2005-2541) | GNU tar setuid/setgid extraction warnings | No GNU tar extraction of runtime/user input; no archive-import API. |
| [CVE-2007-5686](https://security-tracker.debian.org/tracker/CVE-2007-5686) | initscripts/login btmp logging permissions | No SSH/login or initscripts authentication log service is started. |
| [CVE-2011-3374](https://security-tracker.debian.org/tracker/CVE-2011-3374) | apt-key master keyring validation | No apt-key use. The build patch has signed Debian index/hash checks; runtime does not install packages. |
| [CVE-2011-4116](https://security-tracker.debian.org/tracker/CVE-2011-4116) | Perl File::Temp symlink handling | No Perl interpreter or Perl temporary-file interface executes on gateway requests. |
| [CVE-2013-4392](https://security-tracker.debian.org/tracker/CVE-2013-4392) | systemd permission/SELinux updates | No systemd manager or permission-update service executes in the runtime. |
| [CVE-2017-18018](https://security-tracker.debian.org/tracker/CVE-2017-18018) | Recursive dereferencing chown/chgrp on changing untrusted paths | Runtime never executes recursive chown/chgrp. Image build sets constant authored paths before accepting any user workload. |
| [CVE-2022-0563](https://security-tracker.debian.org/tracker/CVE-2022-0563) | Readline chfn/chsh with untrusted INPUTRC | No chfn/chsh runtime path or privileged interactive account-management service. |
| [CVE-2023-31437](https://security-tracker.debian.org/tracker/CVE-2023-31437) | Manipulating systemd sealed journal display | Gateway traces are private JSON outbox events, not systemd sealed journals. |
| [CVE-2023-31438](https://security-tracker.debian.org/tracker/CVE-2023-31438) | Truncate and resume systemd journal sealing | No journal sealing API; gateway ledger/outbox use SQLite. |
| [CVE-2023-31439](https://security-tracker.debian.org/tracker/CVE-2023-31439) | Modify historic sealed systemd log entries | No sealed-journal dependency or systemd service. |
| [CVE-2024-56433](https://security-tracker.debian.org/tracker/CVE-2024-56433) | Subordinate UID mappings and conflicting network accounts | No newuidmap/subuid/NFS home or network-account login path; fixed UID 10001 and local state. |
| [CVE-2025-5278](https://security-tracker.debian.org/tracker/CVE-2025-5278) | sort traditional-key parsing | No sort utility invocation with request data or arguments. |
| [CVE-2025-6141](https://security-tracker.debian.org/tracker/CVE-2025-6141) | ncurses termcap entry parsing | No terminal-capability/parser interface in the HTTP/IPC service. |
| [CVE-2025-69720](https://security-tracker.debian.org/tracker/CVE-2025-69720) | ncurses infocmp analysis | No infocmp command execution. |
| [CVE-2026-102473](https://security-tracker.debian.org/tracker/CVE-2026-102473) | dash recursive filename matching | No runtime shell expansion of user content. Fixed authored build shell input has no user filenames/patterns. |
| [CVE-2026-102474](https://security-tracker.debian.org/tracker/CVE-2026-102474) | dash printf/echo Unicode escape expansion | No runtime shell eval/printf of user content. Build commands are constant authored code with no inference keys/user payloads. |
| [CVE-2026-15059](https://security-tracker.debian.org/tracker/CVE-2026-15059) | systemd-oomd process-kill IPC | No oomd daemon/IPC socket or host daemon mount is provided to the service. |
| [CVE-2026-15534](https://security-tracker.debian.org/tracker/CVE-2026-15534) | Perl regex superlinear cache | No Perl regex execution; schema patterns use bounded isolated CPython re. |
| [CVE-2026-16742](https://security-tracker.debian.org/tracker/CVE-2026-16742) | systemd-homed group management | No homed/account-management daemon or API. |
| [CVE-2026-18477](https://security-tracker.debian.org/tracker/CVE-2026-18477) | GNU tar incremental dumpdir restoration race | Gateway restore uses SQLite backup to a fresh private target, not tar restoration. |
| [CVE-2026-18508](https://security-tracker.debian.org/tracker/CVE-2026-18508) | GNU tar one-top-level hardlink extraction | No tar archive extraction or supplied hardlink targets. |
| [CVE-2026-3184](https://security-tracker.debian.org/tracker/CVE-2026-3184) | login -h and PAM host canonicalization | No login/PAM authentication; gateway bearers use digest comparison. |
| [CVE-2026-40228](https://security-tracker.debian.org/tracker/CVE-2026-40228) | journald ForwardToWall terminal output | No journald/wall service; user fields are typed JSON, without terminal broadcast. |
| [CVE-2026-42250](https://security-tracker.debian.org/tracker/CVE-2026-42250) | bzip2recover crafted recovery file | No recovery utility execution; compression library presence is not use of the recovery command. |
| [CVE-2026-53910](https://security-tracker.debian.org/tracker/CVE-2026-53910) | diff3 crafted diff-program output | No diff3 or user-selected diff program in the service. |
| [CVE-2026-54369](https://security-tracker.debian.org/tracker/CVE-2026-54369) | Privileged pathname-based libacl operations | No ACL API or user-supplied privileged path operation; state/files use private descriptor checks under the service UID. |
| [CVE-2026-54370](https://security-tracker.debian.org/tracker/CVE-2026-54370) | Privileged getfacl/setfacl/chacl symlink race | None of those commands is invoked on requests; no elevated ACL-management surface. |
| [CVE-2026-54371](https://security-tracker.debian.org/tracker/CVE-2026-54371) | Privileged getfattr/setfattr path traversal | Neither command nor a privileged extended-attribute-management interface executes. |
| [CVE-2026-54411](https://security-tracker.debian.org/tracker/CVE-2026-54411) | pam_userdb plaintext password timing | No PAM/userdb authentication. Bearer comparison uses hmac.compare_digest. |
| [CVE-2026-56391](https://security-tracker.debian.org/tracker/CVE-2026-56391) | uniq multibyte check-chars input | No uniq command path; no arbitrary shell tools. |
| [CVE-2026-56392](https://security-tracker.debian.org/tracker/CVE-2026-56392) | unexpand huge tab-stop argument | No unexpand invocation or command argument surface. |
| [CVE-2026-5704](https://security-tracker.debian.org/tracker/CVE-2026-5704) | GNU tar hidden-file archive interpretation | No untrusted GNU tar/preinspection workflow. Frozen package installation uses reviewed wheel hashes. |
| [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642) | Privileged mount helper/post-mount hooks | No mount API/command; caps are dropped and no-new-privileges is required. |
| [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408) | Privileged nsenter join-cgroup execution | No nsenter or arbitrary process/namespace execution from the API. |
| [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409) | fstab-authorized mount subdirectory traversal | No mount API or privileged fstab workflow. |
| [CVE-2026-78410](https://security-tracker.debian.org/tracker/CVE-2026-78410) | Privileged restricted bind-mount source race | No mount/source-ownership mutation interface; no runtime privilege acquisition. |
| [CVE-2026-82560](https://security-tracker.debian.org/tracker/CVE-2026-82560) | Perl POD document formatting | No POD formatter or untrusted documentation processing in the service. |
| [CVE-2026-9538](https://security-tracker.debian.org/tracker/CVE-2026-9538) | Perl Archive::Tar entry allocation | No Perl archive parsing or archive-upload API. |

The runtime entrypoint is the fixed installed Python gateway; provider/validation
children are owned fixed Python processes. No user function, shell/local tool,
archive, custom SDK or native-code execution is exposed. Compose mounts private
project config/state/IPC only, with no host daemon/Docker socket or consumer
checkout. It requires UID 10001, dropped capabilities, read-only root and
no-new-privileges. Host/root administration remains outside this profile.

The build is a separate trusted-code activity: official digest-pinned bases,
reviewed frozen wheel hashes, the authored package and signed-index/hash-verified
security patches. No consumer secrets or requests are build inputs. That does
not approve building arbitrary projects/archives or invoking vulnerable utilities
manually. Evidence includes the locked Dockerfile, security patch provenance,
actual nonroot/fake/egress checks, child process ownership and API control denial.

Final assessment must match actual source/artifact/package versions, both intended
architectures and current raw findings. New local-tool execution, daemon mounts,
privileges, mutable installed code or altered build inputs invalidate this review.
No CVE suppression is introduced by this document.
