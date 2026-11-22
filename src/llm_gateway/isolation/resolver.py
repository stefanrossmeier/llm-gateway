"""Reject oversized Linux search lists before entering the libc resolver."""

import os
import stat
import sys


def check_search_configuration():
    """Mitigate CVE-2026-8674 under the trusted-host configuration boundary.

    Linux host/root configuration may not be changed by API callers. This check
    precedes every owned lookup; it does not defend against a malicious host admin
    racing replacement of /etc/resolv.conf after validation.
    """
    if not sys.platform.startswith("linux"):
        return
    if "LOCALDOMAIN" in os.environ:
        raise ValueError("resolver_search_configuration_denied")
    fd = None
    try:
        fd = os.open("/etc/resolv.conf", os.O_RDONLY | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o022
            or info.st_size > 16384
        ):
            raise ValueError()
        data = os.read(fd, 16385)
        if len(data) > 16384 or b"\0" in data:
            raise ValueError()
        for line in data.decode("ascii").splitlines():
            words = line.split("#", 1)[0].split(";", 1)[0].split()
            if words and words[0] in {"search", "domain"}:
                # Strictly below the libc ~200-character assertion trigger,
                # including separators. No enormous individual or combined list.
                if len(" ".join(words[1:])) > 128:
                    raise ValueError()
    except (OSError, UnicodeError, ValueError):
        raise ValueError("resolver_search_configuration_denied") from None
    finally:
        if fd is not None:
            os.close(fd)
