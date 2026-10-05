from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from vibe.core.config._defaults import DEFAULT_CONSOLE_BASE_URL
from vibe.setup.auth.whoami import WhoAmIResult, fetch_whoami

_WHOAMI_TIMEOUT_SECONDS = 10.0

type WhoAmILookup = Callable[[str], Awaitable[WhoAmIResult | None]]


async def _lookup(api_key: str) -> WhoAmIResult | None:
    return await fetch_whoami(
        DEFAULT_CONSOLE_BASE_URL, api_key, timeout=_WHOAMI_TIMEOUT_SECONDS
    )


def _describe(who: WhoAmIResult | None) -> str:
    if who is None:
        return "account unknown (key rejected or console unreachable)"
    details = [f"account={who.customer_id or 'unknown'}", f"plan={who.plan_name}"]
    if who.organization_kind:
        details.append(f"org={who.organization_kind}")
    return " ".join(details)


async def format_key_accounts(
    keys: Sequence[str], lookup: WhoAmILookup = _lookup
) -> list[str]:
    """One ``key  account`` line per key, flagging keys that share an account."""
    results = await asyncio.gather(*(lookup(key) for key in keys))
    first_by_account: dict[str, int] = {}
    lines: list[str] = []
    for position, (key, who) in enumerate(zip(keys, results, strict=True), start=1):
        line = f"{key}  {_describe(who)}"
        if who is not None and who.customer_id:
            first = first_by_account.setdefault(who.customer_id, position)
            if first != position:
                line += f"  DUPLICATE of #{first}"
        lines.append(line)
    return lines
