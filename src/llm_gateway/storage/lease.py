"""One local service owner per private state root, acquired before SQLite setup."""

import fcntl
import os
import stat
from pathlib import Path

from .sqlite import StoreError, verify_private_ancestors


class StateLease:
    def __init__(self, root: Path):
        self.fd = None
        if root.is_symlink():
            raise StoreError("state_directory_symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = root.resolve()
        verify_private_ancestors(self.root)
        fd = os.open(self.root / "service.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise StoreError("state_lease_not_private")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StoreError("state_service_already_owned") from None
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            os.fsync(fd)
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
