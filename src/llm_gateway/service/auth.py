"""Tokens bind trusted project/principal grants; request labels never grant access."""

import hashlib
import hmac
import os
import socket
import stat
from pathlib import Path

from llm_gateway.config.models import PrincipalConfig
from llm_gateway.config.secrets import read_private_secret
from llm_gateway.storage.sqlite import verify_private_ancestors

from .engine import Authority, EngineError


class Authenticator:
    def __init__(self, registry, *, owner_socket=None, ipc_project=None):
        self.registry, self.ipc_project = registry, ipc_project
        self.ipc_verified = False
        if owner_socket is not None:
            if owner_socket.family != socket.AF_UNIX:
                raise EngineError("owner_ipc_requires_unix_socket")
            info = os.stat(owner_socket.getsockname(), follow_symlinks=False)
            verify_private_ancestors(Path(owner_socket.getsockname()).parent.resolve())
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise EngineError("owner_ipc_socket_not_private")
            self.ipc_verified = True

    def authenticate(self, authorization, *, client):
        if authorization is None and self.ipc_verified and client is None and self.ipc_project:
            snapshot = self.registry.get(self.ipc_project)
            config = snapshot.config
            if config.access.owner_only_ipc:
                return Authority(
                    config.project_id,
                    "owner-ipc",
                    PrincipalConfig(
                        token_file="/not-used-for-owner-ipc",
                        aliases=list(config.aliases),
                        operations=sorted({alias.operation for alias in config.aliases.values()}),
                        permissions=[
                            "infer",
                            "receipts:own",
                            "usage:project",
                            "admin:reload",
                            "admin:reconcile",
                            "admin:backup",
                        ],
                    ),
                    snapshot.digest,
                    owner_ipc=True,
                )
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise EngineError("authentication_required")
        value = authorization[7:]
        if not 32 <= len(value.encode()) <= 16384:
            raise EngineError("authentication_failed")
        candidate = hashlib.sha256(value.encode()).digest()
        matches = []
        with self.registry.store.connection() as connection:
            projects = [
                row[0] for row in connection.execute("SELECT project_id FROM active_configs")
            ]
        for project in projects:
            snapshot = self.registry.get(project)
            config = snapshot.config
            for principal, grants in config.access.principals.items():
                if principal == "owner-ipc":
                    continue  # Historical configs cannot alias a bearer to private IPC history.
                try:
                    token = read_private_secret(grants.token_file)
                except ValueError:
                    continue
                if len(token.encode()) < 32:
                    continue
                if hmac.compare_digest(candidate, hashlib.sha256(token.encode()).digest()):
                    matches.append(Authority(project, principal, grants, snapshot.digest))
        if len(matches) != 1:
            raise EngineError("authentication_failed")
        return matches[0]


def require(authority, permission):
    if permission not in authority.grants.permissions:
        raise EngineError("permission_forbidden")
