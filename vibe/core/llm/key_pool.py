"""Multi-account API key pool with automatic failover.

Several accounts can share one provider by listing their keys, comma separated,
in the plural variant of the provider's key variable (``VIBE_API_KEYS`` next
to ``VIBE_API_KEY``). The pool wraps the HTTP transport, so failover happens
below the SDK: a request refused because one account is rate limited, out of
quota or revoked is replayed on the next account before the SDK or the agent
loop ever sees an error. The same code serves ``vibe`` and ``vibe-acp``.

Accounts that fail are put in cooldown, and the cooldowns are shared between
processes through a small state file in ``VIBE_HOME``, so a fresh ``vibe-acp``
does not start by hammering an account an earlier process just exhausted.
Keys are never logged; accounts are reported by their position in the list.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import email.utils
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any

import httpx

from vibe.utils.api_keys import pooled_keys

try:  # POSIX only; without it the state file is still written atomically.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger("vibe")

_STATE_FILE_NAME = "key_pool_state.json"
_STATE_LOCK_NAME = "key_pool_state.lock"

# Rate limit without a Retry-After: wait this long, doubling on each further
# failure of the same account, up to the cap below.
_RATE_LIMIT_BASE_COOLDOWN = 30.0
_RATE_LIMIT_MAX_COOLDOWN = 6 * 3600.0
# A 429 that talks about a quota (as opposed to a per-minute limit) will not
# clear soon, so skip the escalation and park the account for a long while.
_QUOTA_COOLDOWN = 6 * 3600.0
_PAYMENT_COOLDOWN = 6 * 3600.0
_AUTH_COOLDOWN = 24 * 3600.0
_FORBIDDEN_COOLDOWN = 3600.0
_QUOTA_PATTERN = re.compile(r"quota|monthly|daily|credit|billing", re.IGNORECASE)
_MAX_BODY_BYTES = 8192

_FAILOVER_STATUSES = frozenset({401, 402, 403, 429})


# Tokens spent per key in this process, for the summary shown on exit.
_SESSION_USAGE: dict[str, list[int]] = {}


_MASK_EDGE = 3


def mask_key(key: str) -> str:
    if len(key) <= 2 * _MASK_EDGE:
        return "*" * len(key)
    hidden = "*" * (len(key) - 2 * _MASK_EDGE)
    return f"{key[:_MASK_EDGE]}{hidden}{key[-_MASK_EDGE:]}"


def record_key_usage(key: str, input_tokens: int, output_tokens: int) -> None:
    if not (input_tokens or output_tokens):
        return
    counts = _SESSION_USAGE.setdefault(key, [0, 0])
    counts[0] += input_tokens
    counts[1] += output_tokens


# Key the Unified harness resolved for its latest model call. That path does not
# go through the pool's transport, so this stands in for the lease.
_ACTIVE_KEY: str | None = None


def note_active_key(key: str | None) -> None:
    global _ACTIVE_KEY
    _ACTIVE_KEY = key


def record_active_key_usage(input_tokens: int, output_tokens: int) -> None:
    if _ACTIVE_KEY:
        record_key_usage(_ACTIVE_KEY, input_tokens, output_tokens)


def session_key_usage() -> list[tuple[str, int, int]]:
    """(masked key, input tokens, output tokens) for each pooled key used."""
    return [
        (mask_key(key), counts[0], counts[1]) for key, counts in _SESSION_USAGE.items()
    ]


def _fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _vibe_home() -> Path:
    from vibe.core.paths import VIBE_HOME

    return VIBE_HOME.path


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


@dataclass(frozen=True, slots=True)
class KeyLease:
    key: str
    index: int


class KeyPool:
    """Ordered accounts, one active at a time, with persisted cooldowns."""

    def __init__(
        self,
        keys: Iterable[str],
        *,
        state_dir: Path | Callable[[], Path] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        unique = list(dict.fromkeys(key for key in keys if key))
        self._keys = unique
        self._fingerprints = [_fingerprint(key) for key in unique]
        self._clock = clock
        self._state_dir = state_dir
        self._last_index: int | None = None
        self._active_index: int | None = None

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self._keys)

    def contains(self, key: str) -> bool:
        return key in self._keys

    @classmethod
    def from_environment(
        cls,
        api_key_env_var: str,
        *,
        primary: str | None = None,
        environ: Mapping[str, str] | None = None,
        state_dir: Path | Callable[[], Path] | None = None,
    ) -> KeyPool | None:
        """A pool of ``primary`` plus the plural variable's keys, or ``None``.

        Fewer than two distinct keys is not worth wrapping the transport for.
        """
        keys = pooled_keys(api_key_env_var, environ)
        if primary and primary not in keys:
            keys.insert(0, primary)
        pool = cls(keys, state_dir=state_dir)
        return pool if len(pool) > 1 else None

    # -- persisted state ---------------------------------------------------

    def _state_path(self) -> Path | None:
        try:
            base = self._state_dir() if callable(self._state_dir) else self._state_dir
            return (base or _vibe_home()) / _STATE_FILE_NAME
        except Exception:
            logger.debug("Key pool state directory unavailable", exc_info=True)
            return None

    def _locked(self, path: Path):
        return _StateLock(path.with_name(_STATE_LOCK_NAME))

    def _load(self, path: Path | None) -> dict[str, Any]:
        if path is None:
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _store(self, path: Path | None, state: dict[str, Any]) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".key_pool_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(state, handle)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError:
            logger.debug("Could not persist key pool state", exc_info=True)

    def _accounts(self, state: dict[str, Any]) -> dict[str, dict[str, float]]:
        accounts = state.get("accounts")
        return accounts if isinstance(accounts, dict) else {}

    # -- selection ---------------------------------------------------------

    def acquire(self, exclude: Iterable[str] = ()) -> KeyLease | None:
        """The account to use next, skipping ``exclude`` and accounts cooling.

        Sticky: the account that last worked stays first, so one account is
        used up before the next is touched. When every remaining account is
        cooling, the one that recovers soonest is returned for a single try.
        """
        excluded = set(exclude)
        path = self._state_path()
        now = self._clock()
        state = self._load(path)
        accounts = self._accounts(state)
        current = state.get("current")
        start = (
            self._fingerprints.index(current) if current in self._fingerprints else 0
        )
        order = [
            (start + offset) % len(self._keys) for offset in range(len(self._keys))
        ]

        soonest: tuple[float, int] | None = None
        for index in order:
            if self._keys[index] in excluded:
                continue
            until = float(accounts.get(self._fingerprints[index], {}).get("until", 0))
            if until <= now:
                return KeyLease(self._keys[index], index)
            if soonest is None or until < soonest[0]:
                soonest = (until, index)
        if soonest is not None and not excluded:
            return KeyLease(self._keys[soonest[1]], soonest[1])
        return None

    def report_success(self, lease: KeyLease) -> None:
        fingerprint = self._fingerprints[lease.index]
        path = self._state_path()
        if path is None:
            self._note_switch(lease.index)
            return
        with self._locked(path):
            state = self._load(path)
            accounts = self._accounts(state)
            changed = state.get("current") != fingerprint
            if accounts.get(fingerprint, {}).get("fails"):
                accounts[fingerprint] = {"until": 0, "fails": 0}
                changed = True
            if changed:
                state["current"] = fingerprint
                state["accounts"] = accounts
                self._store(path, state)
        self._note_switch(lease.index)

    def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Attribute tokens to the account that served the latest request."""
        if self._active_index is None:
            return
        record_key_usage(self._keys[self._active_index], input_tokens, output_tokens)

    def note_active(self, key: str) -> None:
        """Remember which account the request now in flight is sent with."""
        if key in self._keys:
            self._active_index = self._keys.index(key)

    def report_failure(
        self, lease: KeyLease, status: int, *, retry_after: float | None, body: str
    ) -> float:
        """Park the account; returns how many seconds it will be skipped."""
        fingerprint = self._fingerprints[lease.index]
        path = self._state_path()
        now = self._clock()
        cooldown = 0.0
        if path is None:
            return _RATE_LIMIT_BASE_COOLDOWN
        with self._locked(path):
            state = self._load(path)
            accounts = self._accounts(state)
            entry = accounts.get(fingerprint, {})
            fails = int(entry.get("fails", 0)) + 1
            cooldown = self._cooldown_for(status, retry_after, body, fails)
            accounts[fingerprint] = {"until": now + cooldown, "fails": fails}
            state["accounts"] = accounts
            self._store(path, state)
        logger.warning(
            "API key pool: account #%d refused with HTTP %d, skipping it for %ds",
            lease.index + 1,
            status,
            int(cooldown),
        )
        return cooldown

    @staticmethod
    def _cooldown_for(
        status: int, retry_after: float | None, body: str, fails: int
    ) -> float:
        match status:
            case 401:
                return _AUTH_COOLDOWN
            case 402:
                return _PAYMENT_COOLDOWN
            case 403:
                return _FORBIDDEN_COOLDOWN
        if body and _QUOTA_PATTERN.search(body):
            return _QUOTA_COOLDOWN
        escalated = min(
            _RATE_LIMIT_BASE_COOLDOWN * 2 ** (fails - 1), _RATE_LIMIT_MAX_COOLDOWN
        )
        if retry_after is None:
            return escalated
        wait = max(retry_after, escalated if fails > 1 else 1.0)
        return min(wait, _RATE_LIMIT_MAX_COOLDOWN)

    def _note_switch(self, index: int) -> None:
        if self._last_index is not None and self._last_index != index:
            logger.info(
                "API key pool: switched from account #%d to #%d",
                self._last_index + 1,
                index + 1,
            )
        self._last_index = index


class _StateLock:
    """Best-effort inter-process lock around state read-modify-write."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any = None

    def __enter__(self) -> _StateLock:
        if fcntl is None:
            return self
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self._path, "a+")
            fcntl.flock(self._handle, fcntl.LOCK_EX)
        except OSError:
            self._handle = None
        return self

    def __exit__(self, *exc: object) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle, fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None


def _authorization_key(request: httpx.Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


class KeyPoolTransport(httpx.AsyncBaseTransport):
    """Replays refused requests on the next account of a ``KeyPool``.

    Only requests that carry one of the pool's keys are touched, so calls to
    other services through the same client are left alone. Because this sits
    under the client's response hooks and the SDK's retry loop, an account that
    was swapped out never surfaces as a retry notice.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, pool: KeyPool) -> None:
        self._inner = inner
        self._pool = pool

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        sent_key = _authorization_key(request)
        if (
            sent_key is None
            or not self._pool.contains(sent_key)
            or not _is_replayable(request)
        ):
            if sent_key is not None:
                self._pool.note_active(sent_key)
            return await self._inner.handle_async_request(request)

        tried: set[str] = set()
        lease = self._pool.acquire()
        while lease is not None:
            request.headers["authorization"] = f"Bearer {lease.key}"
            self._pool.note_active(lease.key)
            response = await self._inner.handle_async_request(request)
            if response.status_code not in _FAILOVER_STATUSES:
                if response.status_code < 400:
                    self._pool.report_success(lease)
                return response
            self._pool.report_failure(
                lease,
                response.status_code,
                retry_after=_parse_retry_after(response.headers.get("retry-after")),
                body=await _read_error_body(response),
            )
            tried.add(lease.key)
            lease = self._pool.acquire(exclude=tried)
            if lease is None:
                # Every account refused: hand the last refusal to the caller so
                # the SDK's own retry and backoff take over.
                return response
            await response.aclose()
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _is_replayable(request: httpx.Request) -> bool:
    """Whether the body can be sent again (bytes/JSON/multipart, not a generator)."""
    try:
        request.content  # noqa: B018 - raises RequestNotRead for one-shot streams
    except httpx.RequestNotRead:
        return False
    return True


async def _read_error_body(response: httpx.Response) -> str:
    try:
        await response.aread()
        return response.content[:_MAX_BODY_BYTES].decode("utf-8", errors="replace")
    except Exception:
        logger.debug("Could not read the body of a refused response", exc_info=True)
        return ""
