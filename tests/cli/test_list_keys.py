from __future__ import annotations

from collections.abc import Iterator
import json
from typing import Any

import pytest

from vibe.app_server.models import AccountPlanKind
from vibe.cli import cli as cli_mod
from vibe.cli.list_keys import (
    _describe,
    _load_cached_entry,
    _plan_type,
    _store_cached_entry,
    build_keys_json,
    format_key_accounts,
)
from vibe.core.identity import IdentityResult
from vibe.core.paths import WHOAMI_CACHE_MULTI_FILE
from vibe.setup.auth.whoami import WhoAmIResult


def _who(customer_id: str, plan_name: str = "Free") -> WhoAmIResult:
    return WhoAmIResult(
        plan_type=AccountPlanKind.API, plan_name=plan_name, customer_id=customer_id
    )


def _identity(email: str | None) -> IdentityResult:
    return IdentityResult(id="user-1", email=email)


async def _lookup_nothing(_key: str) -> WhoAmIResult | None:
    return None


@pytest.mark.asyncio
async def test_keys_of_the_same_account_are_flagged_as_duplicates() -> None:
    accounts = {"k1": _who("a"), "k2": _who("b"), "k3": _who("a"), "k4": None}
    identities: dict[str, IdentityResult | None] = {
        "k1": _identity("alice@example.com"),
        "k2": _identity("bob@example.com"),
        "k3": _identity("alice@example.com"),
        "k4": None,
    }

    async def lookup(key: str) -> WhoAmIResult | None:
        return accounts[key]

    async def lookup_identity(key: str) -> IdentityResult | None:
        return identities[key]

    lines = await format_key_accounts(
        list(accounts), lookup=lookup, identity_lookup=lookup_identity
    )

    assert lines[0] == "k1  account=a plan=Free email=alice@example.com"
    assert lines[1] == "k2  account=b plan=Free email=bob@example.com"
    assert (
        lines[2] == "k3  account=a plan=Free email=alice@example.com  DUPLICATE of #1"
    )
    assert lines[3].startswith("k4  key rejected or console unreachable")


@pytest.mark.asyncio
async def test_identity_email_is_included_when_whoami_succeeds() -> None:
    async def lookup(_key: str) -> WhoAmIResult:
        return _who("c1")

    async def lookup_identity(_key: str) -> IdentityResult:
        return _identity("ronan@example.com")

    lines = await format_key_accounts(
        ["k1"], lookup=lookup, identity_lookup=lookup_identity
    )

    assert lines[0] == "k1  account=c1 plan=Free email=ronan@example.com"


@pytest.mark.asyncio
async def test_email_omitted_when_identity_lookup_returns_none() -> None:
    async def lookup(_key: str) -> WhoAmIResult:
        return _who("c1")

    async def lookup_identity(_key: str) -> IdentityResult | None:
        return None

    lines = await format_key_accounts(
        ["k1"], lookup=lookup, identity_lookup=lookup_identity
    )

    assert lines[0] == "k1  account=c1 plan=Free"


def test_describe_omits_email_when_not_present() -> None:
    who = _who("c1")
    identity = IdentityResult(id="user-1", email=None)
    assert _describe(who, identity) == "account=c1 plan=Free"


def test_describe_both_unavailable() -> None:
    assert _describe(None, None) == "key rejected or console unreachable"


def test_plan_type_free() -> None:
    assert _plan_type(_who("c1", "Free")) == "free"
    assert _plan_type(_who("c1", "FREE")) == "free"


def test_plan_type_paid() -> None:
    assert _plan_type(_who("c1", "PAYG")) == "paid"
    assert _plan_type(_who("c1", "Pro")) == "paid"
    assert _plan_type(_who("c1", "Team")) == "paid"


def test_plan_type_unknown_when_whoami_fails() -> None:
    assert _plan_type(None) == "unknown"


def test_gather_pooled_keys_prepends_primary_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pooled = ["mstrl___abce", "mstrl___abcf"]
    monkeypatch.setattr(
        "vibe.utils.api_keys.pooled_keys", lambda *_args, **_kwargs: list(pooled)
    )
    monkeypatch.setattr(
        "vibe.utils.api_keys.resolve_api_key", lambda *_args, **_kwargs: "mstrl___abcd"
    )

    assert cli_mod._gather_pooled_keys() == [
        "mstrl___abcd",
        "mstrl___abce",
        "mstrl___abcf",
    ]


def test_gather_pooled_keys_omits_primary_when_already_pooled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pooled = ["mstrl___abcd", "mstrl___abce"]
    monkeypatch.setattr(
        "vibe.utils.api_keys.pooled_keys", lambda *_args, **_kwargs: list(pooled)
    )
    monkeypatch.setattr(
        "vibe.utils.api_keys.resolve_api_key", lambda *_args, **_kwargs: "mstrl___abcd"
    )

    assert cli_mod._gather_pooled_keys() == ["mstrl___abcd", "mstrl___abce"]


def test_print_export_keys_outputs_comma_separated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli_mod,
        "_gather_pooled_keys",
        lambda: ["mstrl___abcd", "mstrl___abce", "mstrl___abcf"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_mod._print_export_keys()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == "mstrl___abcd,mstrl___abce,mstrl___abcf"


def test_print_export_keys_empty_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_mod, "_gather_pooled_keys", lambda: [])

    with pytest.raises(SystemExit) as exc_info:
        cli_mod._print_export_keys()

    assert exc_info.value.code == 1
    assert capsys.readouterr().out.strip() == ""


@pytest.mark.asyncio
async def test_build_keys_json_structure() -> None:
    async def lookup(_key: str) -> WhoAmIResult:
        return _who("c1", "FREE")

    async def lookup_identity(key: str) -> IdentityResult:
        return _identity(f"owner-{key[-1]}@example.com")

    result: Any = await build_keys_json(
        ["mstrl_abcd", "mstrl_abce"], lookup=lookup, identity_lookup=lookup_identity
    )

    assert result["vibe"]["protocol"] == "vibe"
    assert "endpoint" in result["vibe"]
    assert "userAgent" in result["vibe"]
    keys = result["vibe"]["keys"]
    assert keys[0]["key"] == "mstrl_abcd"
    assert keys[0]["owner"] == "owner-d@example.com"
    assert keys[0]["type"] == "free"
    assert keys[1]["key"] == "mstrl_abce"
    assert keys[1]["owner"] == "owner-e@example.com"


@pytest.mark.asyncio
async def test_build_keys_json_unknown_type_when_whoami_fails() -> None:
    async def lookup_identity(_key: str) -> IdentityResult:
        return _identity("owner@example.com")

    result: Any = await build_keys_json(
        ["mstrl_xyz"], lookup=_lookup_nothing, identity_lookup=lookup_identity
    )

    entry = result["vibe"]["keys"][0]
    assert entry["key"] == "mstrl_xyz"
    assert entry["owner"] == "owner@example.com"
    assert entry["type"] == "unknown"


@pytest.mark.asyncio
async def test_build_keys_json_empty_keys() -> None:
    result: Any = await build_keys_json([])

    assert result["vibe"]["keys"] == []


def test_print_export_keys_json_outputs_formatted_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_mod, "_gather_pooled_keys", lambda: ["mstrl_abcd"])

    expected = {
        "vibe": {
            "protocol": "vibe",
            "endpoint": "https://api.mistral.ai/v1/chat/completions",
            "userAgent": "Mistral-Vibe/0.0.0",
            "keys": [
                {"key": "mstrl_abcd", "owner": "test@example.com", "type": "free"}
            ],
        }
    }

    async def fake_build_keys_json(_keys: Any) -> Any:
        return expected

    monkeypatch.setattr("vibe.cli.list_keys.build_keys_json", fake_build_keys_json)

    with pytest.raises(SystemExit) as exc_info:
        cli_mod._print_export_keys_json()

    assert exc_info.value.code == 0
    assert json.loads(capsys.readouterr().out) == expected


def test_print_export_keys_json_empty_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_mod, "_gather_pooled_keys", lambda: [])

    expected = {
        "vibe": {"protocol": "vibe", "endpoint": "", "userAgent": "", "keys": []}
    }

    async def fake_build_keys_json(_keys: Any) -> Any:
        return expected

    monkeypatch.setattr("vibe.cli.list_keys.build_keys_json", fake_build_keys_json)

    with pytest.raises(SystemExit) as exc_info:
        cli_mod._print_export_keys_json()

    assert exc_info.value.code == 1
    assert json.loads(capsys.readouterr().out) == expected


@pytest.fixture
def _clear_multi_cache() -> Iterator[None]:
    path = WHOAMI_CACHE_MULTI_FILE.path
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


def test_store_and_load_cached_entry_round_trips(_clear_multi_cache: None) -> None:
    who = _who("c1", "FREE")
    identity = _identity("ronan@example.com")

    _store_cached_entry("test-key", who=who, identity=identity)

    cached = _load_cached_entry("test-key")
    assert cached is not None
    assert _parse_whoami_cached(cached) == who
    assert _parse_identity_cached(cached) == identity


def test_load_cached_entry_returns_none_when_missing(_clear_multi_cache: None) -> None:
    assert _load_cached_entry("nonexistent-key") is None


def test_load_cached_entry_returns_none_when_stale(_clear_multi_cache: None) -> None:
    from vibe.cli.list_keys import _CACHE_TTL_SECONDS

    who = _who("c1", "FREE")
    _store_cached_entry("test-key", who=who)

    path = WHOAMI_CACHE_MULTI_FILE.path
    entries = json.loads(path.read_text())
    for entry in entries:
        if entry.get("key_hash") == _hash_for_test("test-key"):
            entry["stored_at_timestamp"] -= _CACHE_TTL_SECONDS + 1
    path.write_text(json.dumps(entries))

    assert _load_cached_entry("test-key") is None


def test_store_cached_entry_merges_whoami_and_identity(
    _clear_multi_cache: None,
) -> None:
    who = _who("c1", "FREE")
    identity = _identity("ronan@example.com")

    _store_cached_entry("test-key", who=who)
    _store_cached_entry("test-key", identity=identity)

    cached = _load_cached_entry("test-key")
    assert cached is not None
    assert _parse_whoami_cached(cached) == who
    assert _parse_identity_cached(cached) == identity


def _hash_for_test(api_key: str) -> str:
    from vibe.cli.list_keys import _hash_api_key

    return _hash_api_key(api_key)


def _parse_whoami_cached(entry: dict[str, Any]) -> WhoAmIResult | None:
    from vibe.cli.list_keys import _parse_whoami_from_cache

    return _parse_whoami_from_cache(entry)


def _parse_identity_cached(entry: dict[str, Any]) -> IdentityResult | None:
    from vibe.cli.list_keys import _parse_identity_from_cache

    return _parse_identity_from_cache(entry)


@pytest.mark.asyncio
async def test_lookup_uses_cache_on_second_call(
    monkeypatch: pytest.MonkeyPatch, _clear_multi_cache: None
) -> None:
    from vibe.cli.list_keys import _lookup

    who = _who("c1", "FREE")
    _store_cached_entry("cached-key", who=who)

    calls: list[str] = []

    async def fake_fetch_whoami(
        *_args: object, **_kwargs: object
    ) -> WhoAmIResult | None:
        calls.append("fetch")
        raise AssertionError("should not fetch when cached")

    monkeypatch.setattr("vibe.cli.list_keys.fetch_whoami", fake_fetch_whoami)

    result = await _lookup("cached-key")
    assert result == who
    assert calls == []


@pytest.mark.asyncio
async def test_lookup_identity_uses_cache_on_second_call(
    monkeypatch: pytest.MonkeyPatch, _clear_multi_cache: None
) -> None:
    from vibe.cli.list_keys import _lookup_identity

    identity = _identity("ronan@example.com")
    _store_cached_entry("cached-key", identity=identity)

    class _NeverCalled:
        async def read(self, **kwargs: object) -> IdentityResult:
            raise AssertionError("should not fetch when cached")

    monkeypatch.setattr("vibe.cli.list_keys.HttpIdentityGateway", _NeverCalled)

    result = await _lookup_identity("cached-key")
    assert result == identity


@pytest.mark.asyncio
async def test_build_keys_json_uses_cached_data(_clear_multi_cache: None) -> None:

    who = _who("c1", "FREE")
    identity = _identity("ronan@example.com")
    _store_cached_entry("mstrl_cached", who=who, identity=identity)

    result: Any = await build_keys_json(["mstrl_cached"])

    entry = result["vibe"]["keys"][0]
    assert entry["key"] == "mstrl_cached"
    assert entry["owner"] == "ronan@example.com"
    assert entry["type"] == "free"
