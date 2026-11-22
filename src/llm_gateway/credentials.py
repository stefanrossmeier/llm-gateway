"""Private local secret-file reading shared by lean clients and service config."""

import os
import secrets
import stat
import tempfile
from pathlib import Path


class SecretUnavailable(ValueError):
    def __init__(self):
        super().__init__("credential_unavailable")


def read_private_secret(path: str) -> str:
    fd = None
    try:
        if os.name != "posix":
            raise SecretUnavailable()
        if not os.path.isabs(path):
            raise SecretUnavailable()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in {os.getuid(), 0}:
            raise SecretUnavailable()
        if info.st_mode & 0o077 or info.st_size > 16384:
            raise SecretUnavailable()
        raw = os.read(fd, 16385)
        value = raw.decode("utf-8").strip()
        if not value or "\n" in value or "\r" in value or len(raw) > 16384:
            raise SecretUnavailable()
        return value
    except (OSError, UnicodeError) as error:
        raise SecretUnavailable() from error
    finally:
        if fd is not None:
            os.close(fd)


def provision_gateway_token(path: Path, *, rotate=False) -> None:
    """Explicit operator action. Never return/log the cryptographic token value."""
    from llm_gateway.storage.sqlite import verify_private_ancestors

    if not path.is_absolute():
        raise ValueError("token_path_must_be_absolute")
    verify_private_ancestors(path.parent)
    if rotate:
        read_private_secret(str(path))
    temporary, created = None, False
    try:
        if rotate:
            fd, temporary = tempfile.mkstemp(prefix=".gateway-token-", dir=path.parent)
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(48) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if rotate:
            os.replace(temporary, path)
            temporary = None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        if temporary is not None:
            os.unlink(temporary)
        if created:
            path.unlink(missing_ok=True)
        raise ValueError("token_provision_failed") from None
