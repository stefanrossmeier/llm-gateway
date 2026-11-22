"""Explicit provider credential reference resolution, with file precedence."""

import os
from collections.abc import Mapping

from llm_gateway.credentials import SecretUnavailable as SecretUnavailable
from llm_gateway.credentials import read_private_secret as read_private_secret

from .models import CredentialReference


def resolve_credential(
    reference: CredentialReference, environment: Mapping[str, str] | None = None
) -> str:
    env = os.environ if environment is None else environment
    selected_file = env.get(reference.file_env_var, "").strip() if reference.file_env_var else ""
    if selected_file or reference.file_path:
        return read_private_secret(selected_file or reference.file_path)
    value = env.get(reference.env_var, "").strip() if reference.env_var else ""
    if not value or "\n" in value or "\r" in value or len(value) > 16384:
        raise SecretUnavailable()
    return value
