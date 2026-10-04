from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
import re

from vibe.utils.keyring import get_api_key_from_keyring


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
    plural = f"{env_key}S"
    if pooled := split_keys(os.environ.get(plural)):
        return pooled[0], ApiKeyOrigin(ApiKeySource.ENVIRONMENT, plural)
    return None


def resolve_api_key(env_key: str) -> str | None:
    resolved = resolve_api_key_with_origin(env_key)
    return resolved[0] if resolved else None
