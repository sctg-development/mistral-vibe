from __future__ import annotations

import pytest

from vibe.cli import cli as cli_mod
from vibe.cli.list_keys import format_key_accounts
from vibe.setup.auth.whoami import WhoAmIResult


def _who(customer_id: str) -> WhoAmIResult:
    return WhoAmIResult(plan_type="api", plan_name="Free", customer_id=customer_id)


@pytest.mark.asyncio
async def test_keys_of_the_same_account_are_flagged_as_duplicates() -> None:
    accounts = {"k1": _who("a"), "k2": _who("b"), "k3": _who("a"), "k4": None}

    async def lookup(key: str) -> WhoAmIResult | None:
        return accounts[key]

    lines = await format_key_accounts(list(accounts), lookup)

    assert lines[0] == "k1  account=a plan=Free"
    assert lines[1] == "k2  account=b plan=Free"
    assert lines[2] == "k3  account=a plan=Free  DUPLICATE of #1"
    assert lines[3].startswith("k4  account unknown")


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
