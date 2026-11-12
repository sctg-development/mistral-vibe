from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
import os
import re
from types import MappingProxyType

from keyring.errors import KeyringError

from vibe.utils.keyring import get_api_key_from_keyring, set_api_key_in_keyring
from vibe.utils.oidc_marker import oidc_enabled
from vibe.utils.secrets import holding_secrets

POOL_ENV_SUFFIX = "S"


class ApiKeySource(StrEnum):
    """The places ``resolve_api_key`` looks, in the order it looks."""

    SESSION = "session"
    ENVIRONMENT = "environment"
    KEYRING = "keyring"
    OIDC = "oidc"


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
        match self.source:
            case ApiKeySource.SESSION:
                return f"the run's credential {self.env_var}"
            case ApiKeySource.ENVIRONMENT:
                return f"env var {self.env_var}"
            case ApiKeySource.KEYRING:
                return "the keyring"
            case ApiKeySource.OIDC:
                return "the CLI OIDC session"


# The credentials of the code running in this context, by environment variable
# name. Asyncio tasks and to_thread workers inherit it, so one process can run
# sessions that each send their own key.
_session_credentials: ContextVar[Mapping[str, str] | None] = ContextVar(
    "session_credentials", default=None
)


@contextmanager
def session_credentials(values: Mapping[str, str]) -> Iterator[None]:
    """Resolve API keys from ``values`` first, inside this context only.

    ``values`` maps an environment variable name, as a provider's
    ``api_key_env_var`` names it, to its value. They are never written to
    ``os.environ``, and the logs redact them while the context lasts. Must be
    entered and left in the same context, as a ``ContextVar`` is.
    """
    with holding_secrets(values.values()):
        token = _session_credentials.set(MappingProxyType(dict(values)))
        try:
            yield
        finally:
            _session_credentials.reset(token)


def active_api_key(
    *, env_value: str | None, keyring_value: str | None
) -> tuple[ApiKeySource, str] | None:
    """The winning credential and its source, or ``None`` when neither holds one.

    The precedence rule shared by resolution and ``/status``: a truthy
    environment value wins, otherwise the keyring decides. Inputs are
    already-collected values, so callers own when (and whether) the keyring
    is read — resolution touches it only when the environment holds nothing.
    """
    if env_value:
        return ApiKeySource.ENVIRONMENT, env_value
    if keyring_value:
        return ApiKeySource.KEYRING, keyring_value
    return None


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


def uses_cli_oidc_credential(env_key: str) -> bool:
    """Whether ``env_key`` resolves from the CLI OIDC session, never an API key.

    Shared by resolution and ``/status``, so the reported source cannot claim
    an API-key source that resolution never reads.
    """
    # fork: the Vibe sign-in key is renamed VIBE_API_KEY here, so both names mean "the Mistral key".
    return env_key in {"MISTRAL_API_KEY", "VIBE_API_KEY"} and oidc_enabled()


def _first_pooled_key(env_key: str) -> tuple[str, ApiKeyOrigin] | None:
    """The first pooled account, standing in for a missing single key.

    Multi-account setups may only define the plural variable
    (``VIBE_API_KEYS``); its first key stands in for the single one so the
    rest of the CLI (account lookups, auth state) keeps working.
    """
    plural = pool_env_var(env_key)
    if pooled := split_keys(os.environ.get(plural)):
        return pooled[0], ApiKeyOrigin(ApiKeySource.ENVIRONMENT, plural)
    if stored := stored_pool_keys(env_key):
        return stored[0], ApiKeyOrigin(ApiKeySource.KEYRING, plural)
    return None


def resolve_api_key_with_origin(env_key: str) -> tuple[str, ApiKeyOrigin] | None:
    """The key for ``env_key`` and where it was read from, or ``None``.

    The one place the precedence lives, so a key and its reported origin can
    never disagree: the session's credentials, then ``active_api_key``'s
    env-wins rule. The keyring read goes through its process-wide cache, so
    asking again after a failed call costs nothing — and happens only when
    neither the session nor the environment holds a value.
    """
    if not env_key:
        return None
    if uses_cli_oidc_credential(env_key):
        from vibe.setup.auth._oidc_runtime import stored_oidc_credential

        token = stored_oidc_credential()
        return (token, ApiKeyOrigin(ApiKeySource.OIDC, env_key)) if token else None
    credentials = _session_credentials.get()
    if credentials is not None and (token := credentials.get(env_key)):
        return token, ApiKeyOrigin(ApiKeySource.SESSION, env_key)
    env_value = os.environ.get(env_key)
    keyring_value = None if env_value else get_api_key_from_keyring(env_key)
    resolved = active_api_key(env_value=env_value, keyring_value=keyring_value)
    if resolved is not None:
        source, token = resolved
        return token, ApiKeyOrigin(source, env_key)
    return _first_pooled_key(env_key)


def resolve_api_key(env_key: str) -> str | None:
    resolved = resolve_api_key_with_origin(env_key)
    return resolved[0] if resolved else None
