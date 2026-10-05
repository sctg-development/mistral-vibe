from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import os
import re

from keyring.errors import KeyringError

from vibe.utils.keyring import get_api_key_from_keyring, set_api_key_in_keyring

POOL_ENV_SUFFIX = "S"


class ApiKeySource(StrEnum):
    """The places ``resolve_api_key`` looks, in the order it looks."""

    ENVIRONMENT = "environment"
    KEYRING = "keyring"


@dataclass(frozen=True, slots=True)
class ApiKeyOrigin:
    """Where one resolved key came from, and under which name.

    ``env_var`` keys both sources, so it alone cannot say which answered. The
    pair can, and the difference is what a user acts on: an environment
    variable may hold a value they never set, while a keyring key is replaced
    by running setup again.
    """

    source: ApiKeySource
    env_var: str

    def describe(self) -> str:
        if self.source is ApiKeySource.ENVIRONMENT:
            return f"env var {self.env_var}"
        return "the keyring"


def split_keys(raw: str | None) -> list[str]:
    """Comma, semicolon or newline separated keys, blanks dropped, order kept."""
    if not raw:
        return []
    return [item.strip() for item in re.split(r"[,;\n]", raw) if item.strip()]


def pool_env_var(api_key_env_var: str) -> str:
    return f"{api_key_env_var}{POOL_ENV_SUFFIX}" if api_key_env_var else ""


def stored_pool_keys(api_key_env_var: str) -> list[str]:
    """Accounts saved by earlier sign-ins (one keyring entry, comma separated)."""
    name = pool_env_var(api_key_env_var)
    if not name:
        return []
    return split_keys(get_api_key_from_keyring(name, search_legacy_services=False))


def pooled_keys(
    api_key_env_var: str, environ: Mapping[str, str] | None = None
) -> list[str]:
    """Every account, in order: the plural variable first, then saved sign-ins.

    The keyring is read only for the real process environment, so callers that
    pass their own mapping stay hermetic.
    """
    name = pool_env_var(api_key_env_var)
    if not name:
        return []
    keys = split_keys((os.environ if environ is None else environ).get(name))
    if environ is None:
        keys += stored_pool_keys(api_key_env_var)
    return list(dict.fromkeys(keys))


def add_pooled_key(api_key_env_var: str, api_key: str) -> bool:
    """Remember ``api_key`` as one more account. False when it cannot be saved."""
    name = pool_env_var(api_key_env_var)
    if not name or not api_key:
        return False
    keys = list(dict.fromkeys([*stored_pool_keys(api_key_env_var), api_key]))
    try:
        set_api_key_in_keyring(name, ",".join(keys))
    except KeyringError:
        return False
    return True


def resolve_api_key_with_origin(env_key: str) -> tuple[str, ApiKeyOrigin] | None:
    """The key for ``env_key`` and where it was read from, or ``None``.

    The one place the precedence lives, so a key and its reported origin can
    never disagree. The keyring read goes through its process-wide cache, so
    asking again after a failed call costs nothing.
    """
    if not env_key:
        return None
    if token := os.environ.get(env_key):
        return token, ApiKeyOrigin(ApiKeySource.ENVIRONMENT, env_key)
    if token := get_api_key_from_keyring(env_key):
        return token, ApiKeyOrigin(ApiKeySource.KEYRING, env_key)
    # Multi-account setups may only define the plural variable
    # (``MISTRAL_API_KEYS``); its first key stands in for the single one so the
    # rest of the CLI (account lookups, auth state) keeps working.
    plural = pool_env_var(env_key)
    if pooled := split_keys(os.environ.get(plural)):
        return pooled[0], ApiKeyOrigin(ApiKeySource.ENVIRONMENT, plural)
    if stored := stored_pool_keys(env_key):
        return stored[0], ApiKeyOrigin(ApiKeySource.KEYRING, plural)
    return None


def resolve_api_key(env_key: str) -> str | None:
    resolved = resolve_api_key_with_origin(env_key)
    return resolved[0] if resolved else None
