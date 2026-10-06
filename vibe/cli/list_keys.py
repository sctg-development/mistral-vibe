from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import json
import logging
import os
import time
from typing import Any, TypedDict

from pydantic import ValidationError

from vibe.core.config._defaults import (
    DEFAULT_CONSOLE_BASE_URL,
    DEFAULT_MISTRAL_SERVER_URL,
)
from vibe.core.identity import (
    HttpIdentityGateway,
    IdentityGatewayUnauthorized,
    IdentityGatewayUnavailable,
    IdentityResult,
)
from vibe.core.paths import WHOAMI_CACHE_MULTI_FILE
from vibe.setup.auth.whoami import WhoAmIResult, fetch_whoami
from vibe.utils.http import get_user_agent

_WHOAMI_TIMEOUT_SECONDS = 10.0
_IDENTITY_TIMEOUT_SECONDS = 10.0
_IDENTITY_CONCURRENCY = 3
_IDENTITY_MAX_RETRIES = 3
_IDENTITY_RETRY_BASE_DELAY = 1.0

_CACHE_TTL_SECONDS = 6 * 60 * 60

_VIBE_API_ENDPOINT = f"{DEFAULT_MISTRAL_SERVER_URL}/v1/chat/completions"

logger = logging.getLogger("vibe")

type WhoAmILookup = Callable[[str], Awaitable[WhoAmIResult | None]]
type IdentityLookup = Callable[[str], Awaitable[IdentityResult | None]]


async def _lookup(api_key: str) -> WhoAmIResult | None:
    cached = _load_cached_entry(api_key)
    if cached is not None:
        return _parse_whoami_from_cache(cached)
    result = await fetch_whoami(
        DEFAULT_CONSOLE_BASE_URL, api_key, timeout=_WHOAMI_TIMEOUT_SECONDS
    )
    if result is not None:
        _store_cached_entry(api_key, who=result)
    return result


_DEFAULT_API_BASE_URL = f"{DEFAULT_MISTRAL_SERVER_URL}/v1"
_IDENTITY_SEMAPHORE = asyncio.Semaphore(_IDENTITY_CONCURRENCY)


async def _lookup_identity(api_key: str) -> IdentityResult | None:
    cached = _load_cached_entry(api_key)
    if cached is not None:
        identity = _parse_identity_from_cache(cached)
        if identity is not None:
            return identity
    gateway = HttpIdentityGateway()
    async with _IDENTITY_SEMAPHORE:
        for attempt in range(_IDENTITY_MAX_RETRIES):
            try:
                result = await gateway.read(
                    base_url=_DEFAULT_API_BASE_URL,
                    api_key=api_key,
                    timeout=_IDENTITY_TIMEOUT_SECONDS,
                )
                _store_cached_entry(api_key, identity=result)
                return result
            except IdentityGatewayUnauthorized:
                return None
            except IdentityGatewayUnavailable:
                if attempt < _IDENTITY_MAX_RETRIES - 1:
                    delay = _IDENTITY_RETRY_BASE_DELAY * (2**attempt)
                    logger.warning(
                        "Identity lookup failed for key ending %s, retrying in %.1fs",
                        api_key[-4:],
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    return None
            except Exception:
                return None
        return None


def _hash_api_key(api_key: str) -> str:
    from vibe.core.experiments.manager import hash_api_key

    return hash_api_key(api_key)


def _read_multi_cache() -> list[dict[str, Any]]:
    cache_path = WHOAMI_CACHE_MULTI_FILE.path
    try:
        with cache_path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_multi_cache(entries: list[dict[str, Any]]) -> None:
    cache_path = WHOAMI_CACHE_MULTI_FILE.path
    tmp_path = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(entries, f, separators=(",", ":"))
        os.replace(tmp_path, cache_path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _load_cached_entry(api_key: str) -> dict[str, Any] | None:
    key_hash = _hash_api_key(api_key)
    now = int(time.time())
    for entry in _read_multi_cache():
        if entry.get("key_hash") == key_hash:
            stored_at = entry.get("stored_at_timestamp")
            if isinstance(stored_at, int) and stored_at > now - _CACHE_TTL_SECONDS:
                return entry
            return None
    return None


def _parse_whoami_from_cache(entry: dict[str, Any]) -> WhoAmIResult | None:
    payload = entry.get("whoami")
    if not isinstance(payload, dict):
        return None
    try:
        return WhoAmIResult.model_validate(payload)
    except (ValueError, ValidationError):
        return None


def _parse_identity_from_cache(entry: dict[str, Any]) -> IdentityResult | None:
    payload = entry.get("identity")
    if not isinstance(payload, dict):
        return None
    try:
        return IdentityResult.model_validate(payload)
    except (ValueError, ValidationError):
        return None


def _store_cached_entry(
    api_key: str,
    who: WhoAmIResult | None = None,
    identity: IdentityResult | None = None,
) -> None:
    if who is None and identity is None:
        return
    key_hash = _hash_api_key(api_key)
    entries = _read_multi_cache()
    existing: dict[str, Any] | None = None
    remaining: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("key_hash") == key_hash:
            existing = entry
        else:
            remaining.append(entry)
    if existing is None:
        existing = {"key_hash": key_hash}
    if who is not None:
        existing["whoami"] = who.model_dump(mode="json")
    if identity is not None:
        existing["identity"] = identity.model_dump(mode="json")
    existing["stored_at_timestamp"] = int(time.time())
    remaining.append(existing)
    _write_multi_cache(remaining)


def _describe(who: WhoAmIResult | None, identity: IdentityResult | None) -> str:
    if who is None and identity is None:
        return "key rejected or console unreachable"
    details: list[str] = []
    if who is not None:
        details.append(f"account={who.customer_id or 'unknown'}")
        details.append(f"plan={who.plan_name}")
        if who.organization_kind:
            details.append(f"org={who.organization_kind}")
    if identity is not None and identity.email:
        details.append(f"email={identity.email}")
    return " ".join(details)


def _plan_type(who: WhoAmIResult | None) -> str:
    if who is None:
        return "unknown"
    return "free" if "FREE" in who.plan_name.upper() else "paid"


def _model_to_entry(model: Any, priority: int) -> _ModelEntry:
    from vibe.core.config.models import ModelConfig

    if isinstance(model, ModelConfig):
        input_modalities: list[str] = ["text"]
        if model.supports_images:
            input_modalities.append("image")
        return _ModelEntry(
            id=model.name,
            usage="chat",
            contextWindow=None,
            maxOutputTokens=None,
            tpmLimit=None,
            priority=priority,
            inputModalities=input_modalities,
            outputModalities=["text"],
        )
    return _ModelEntry(
        id=str(model.get("name", model.get("id", ""))),
        usage="chat",
        contextWindow=None,
        maxOutputTokens=None,
        tpmLimit=None,
        priority=priority,
        inputModalities=["text"],
        outputModalities=["text"],
    )


class _KeyEntry(TypedDict):
    key: str
    owner: str | None
    type: str


class _ModelEntry(TypedDict):
    id: str
    usage: str
    contextWindow: int | None
    maxOutputTokens: int | None
    tpmLimit: int | None
    priority: int
    inputModalities: list[str]
    outputModalities: list[str]


async def format_key_accounts(
    keys: Sequence[str],
    *,
    lookup: WhoAmILookup = _lookup,
    identity_lookup: IdentityLookup = _lookup_identity,
) -> list[str]:
    """One ``key  account`` line per key, flagging keys that share an account."""
    whoami_results, identity_results = await asyncio.gather(
        asyncio.gather(*(lookup(key) for key in keys)),
        asyncio.gather(*(identity_lookup(key) for key in keys)),
    )
    first_by_account: dict[str, int] = {}
    lines: list[str] = []
    for position, (key, who, identity) in enumerate(
        zip(keys, whoami_results, identity_results, strict=True), start=1
    ):
        line = f"{key}  {_describe(who, identity)}"
        if who is not None and who.customer_id:
            first = first_by_account.setdefault(who.customer_id, position)
            if first != position:
                line += f"  DUPLICATE of #{first}"
        lines.append(line)
    return lines


async def build_keys_json(
    keys: Sequence[str],
    *,
    lookup: WhoAmILookup = _lookup,
    identity_lookup: IdentityLookup = _lookup_identity,
    models: Sequence[Any] | None = None,
) -> dict[str, dict[str, object]]:
    """Build a structured JSON-safe dict for ``--export-keys-json``."""
    whoami_results, identity_results = await asyncio.gather(
        asyncio.gather(*(lookup(key) for key in keys)),
        asyncio.gather(*(identity_lookup(key) for key in keys)),
    )
    key_entries: list[_KeyEntry] = []
    for key, who, identity in zip(keys, whoami_results, identity_results, strict=True):
        key_entries.append(
            _KeyEntry(
                key=key,
                owner=identity.email if identity and identity.email else None,
                type=_plan_type(who),
            )
        )
    model_entries: list[_ModelEntry] = []
    if models:
        for idx, model in enumerate(models):
            model_entries.append(_model_to_entry(model, idx * 10))
    return {
        "vibe": {
            "protocol": "vibe",
            "endpoint": _VIBE_API_ENDPOINT,
            "userAgent": get_user_agent("mistral"),
            "keys": key_entries,
            "models": model_entries,
        }
    }
