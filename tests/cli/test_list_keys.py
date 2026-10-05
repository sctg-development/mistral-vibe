from __future__ import annotations

import pytest

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
