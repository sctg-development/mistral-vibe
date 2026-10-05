from __future__ import annotations

from rich import print as rprint

from vibe.app_server import SessionExitSummary
from vibe.app_server.models import TokenUsage
from vibe.core.llm.key_pool import session_key_usage
from vibe.utils.session_id import shorten_session_id


def format_session_usage(usage: TokenUsage) -> str:
    lines = [
        "Total tokens used this session: "
        f"input={usage.input_tokens:,} "
        f"output={usage.output_tokens:,} "
        f"(total={usage.total_tokens:,})"
    ]
    lines.extend(
        f"key {key} : input={input_tokens:,} output={output_tokens:,} "
        f"(total={input_tokens + output_tokens:,})"
        for key, input_tokens, output_tokens in session_key_usage()
    )
    return "\n".join(lines)


def print_session_resume_message(summary: SessionExitSummary | None) -> None:
    if summary is None or summary.session_id is None:
        return

    print()
    print(format_session_usage(summary.usage))
    print()
    rprint("To continue this session, run: [bold dark_orange]vibe --continue[/]")
    session_id = shorten_session_id(summary.session_id)
    rprint(f"Or: [bold dark_orange]vibe --resume {session_id}[/]")
