from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from vibe.core.llm.key_pool import KeyPool, KeyPoolTransport
from vibe.utils.api_keys import resolve_api_key_with_origin, split_keys

KEYS = ["key-a", "key-b", "key-c"]
URL = "https://api.mistral.ai/v1/chat/completions"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class _Upstream:
    """Fake API: answers per key from `script`, records the keys it saw."""

    def __init__(self, script: dict[str, list[httpx.Response]]) -> None:
        self.script = script
        self.seen: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].removeprefix("Bearer ")
        self.seen.append(key)
        queue = self.script.get(key, [httpx.Response(200, json={"ok": key})])
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _client(
    tmp_path: Path, upstream: _Upstream, clock: _Clock, keys: list[str] = KEYS
) -> tuple[httpx.AsyncClient, KeyPool]:
    pool = KeyPool(keys, state_dir=tmp_path, clock=clock)
    transport = KeyPoolTransport(httpx.MockTransport(upstream.handler), pool)
    return httpx.AsyncClient(transport=transport), pool


async def _post(client: httpx.AsyncClient, key: str = KEYS[0]) -> httpx.Response:
    return await client.post(
        URL, json={"model": "m"}, headers={"authorization": f"Bearer {key}"}
    )


def test_split_keys_handles_separators_and_blanks() -> None:
    assert split_keys(" a, b;c\n,, d ") == ["a", "b", "c", "d"]
    assert split_keys("") == []
    assert split_keys(None) == []


def test_plural_variable_stands_in_for_missing_single_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FAKE_API_KEY", raising=False)
    monkeypatch.setenv("FAKE_API_KEYS", "first, second")
    resolved = resolve_api_key_with_origin("FAKE_API_KEY")
    assert resolved is not None
    assert resolved[0] == "first"
    assert resolved[1].env_var == "FAKE_API_KEYS"


def test_pool_needs_two_distinct_keys() -> None:
    assert KeyPool.from_environment("X", primary="a", environ={"XS": "a"}) is None
    pool = KeyPool.from_environment("X", primary="a", environ={"XS": "b,c"})
    assert pool is not None
    assert pool.keys == ("a", "b", "c")


@pytest.mark.asyncio
async def test_rate_limited_account_is_replayed_on_the_next_one(
    tmp_path: Path,
) -> None:
    upstream = _Upstream({"key-a": [httpx.Response(429, headers={"retry-after": "5"})]})
    client, _ = _client(tmp_path, upstream, _Clock())

    response = await _post(client)

    assert response.status_code == 200
    assert response.json() == {"ok": "key-b"}
    assert upstream.seen == ["key-a", "key-b"]


@pytest.mark.asyncio
async def test_failed_account_is_skipped_by_later_requests(tmp_path: Path) -> None:
    upstream = _Upstream({"key-a": [httpx.Response(429)]})
    clock = _Clock()
    client, _ = _client(tmp_path, upstream, clock)

    await _post(client)
    upstream.seen.clear()
    await _post(client)

    assert upstream.seen == ["key-b"]  # sticky on the account that worked


@pytest.mark.asyncio
async def test_cooldown_state_is_shared_between_pools(tmp_path: Path) -> None:
    clock = _Clock()
    first, _ = _client(tmp_path, _Upstream({"key-a": [httpx.Response(429)]}), clock)
    await _post(first)

    upstream = _Upstream({})
    second, _ = _client(tmp_path, upstream, clock)
    await _post(second)

    assert upstream.seen == ["key-b"]


@pytest.mark.asyncio
async def test_account_comes_back_after_cooldown(tmp_path: Path) -> None:
    clock = _Clock()
    upstream = _Upstream(
        {"key-a": [httpx.Response(429, headers={"retry-after": "10"})]}
    )
    client, _ = _client(tmp_path, upstream, clock, keys=["key-a", "key-b"])
    await _post(client)  # a refused, b answers and becomes the sticky account

    clock.now += 11  # a's cooldown is over; b is now the one that refuses
    upstream.script["key-a"] = [httpx.Response(200, json={"ok": "key-a"})]
    upstream.script["key-b"] = [httpx.Response(429, headers={"retry-after": "100"})]
    upstream.seen.clear()
    response = await _post(client)

    assert response.json() == {"ok": "key-a"}
    assert upstream.seen == ["key-b", "key-a"]


@pytest.mark.asyncio
async def test_all_accounts_refused_returns_the_last_refusal(tmp_path: Path) -> None:
    upstream = _Upstream({key: [httpx.Response(429)] for key in KEYS})
    client, _ = _client(tmp_path, upstream, _Clock())

    response = await _post(client)

    assert response.status_code == 429
    assert sorted(upstream.seen) == sorted(KEYS)


@pytest.mark.asyncio
async def test_revoked_key_is_parked_for_a_long_time(tmp_path: Path) -> None:
    clock = _Clock()
    upstream = _Upstream({"key-a": [httpx.Response(401)]})
    client, pool = _client(tmp_path, upstream, clock)
    await _post(client)

    clock.now += 3600
    upstream.seen.clear()
    await _post(client)
    assert "key-a" not in upstream.seen

    clock.now += 24 * 3600
    assert pool.acquire(exclude=[KEYS[1], KEYS[2]]) is not None


@pytest.mark.asyncio
async def test_quota_message_skips_escalation(tmp_path: Path) -> None:
    clock = _Clock()
    quota = httpx.Response(429, json={"message": "Monthly token quota exceeded"})
    client, pool = _client(tmp_path, _Upstream({"key-a": [quota]}), clock)
    await _post(client)

    clock.now += 3600
    assert pool.acquire(exclude=[KEYS[1], KEYS[2]]) is None


@pytest.mark.asyncio
async def test_server_errors_are_not_failed_over(tmp_path: Path) -> None:
    upstream = _Upstream({"key-a": [httpx.Response(503)]})
    client, _ = _client(tmp_path, upstream, _Clock())

    response = await _post(client)

    assert response.status_code == 503
    assert upstream.seen == ["key-a"]


@pytest.mark.asyncio
async def test_foreign_credentials_are_left_alone(tmp_path: Path) -> None:
    upstream = _Upstream({"other-key": [httpx.Response(429)]})
    client, _ = _client(tmp_path, upstream, _Clock())

    response = await _post(client, key="other-key")

    assert response.status_code == 429
    assert upstream.seen == ["other-key"]


@pytest.mark.asyncio
async def test_streamed_refusal_is_replayed_before_any_byte(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].removeprefix("Bearer ")
        if key == "key-a":
            return httpx.Response(429)
        return httpx.Response(200, content=b"data: hello\n\n")

    pool = KeyPool(KEYS, state_dir=tmp_path, clock=_Clock())
    transport = KeyPoolTransport(httpx.MockTransport(handler), pool)
    async with httpx.AsyncClient(transport=transport) as client:
        async with client.stream(
            "POST",
            URL,
            json={"stream": True},
            headers={"authorization": "Bearer key-a"},
        ) as response:
            body = b"".join([chunk async for chunk in response.aiter_bytes()])

    assert response.status_code == 200
    assert body == b"data: hello\n\n"


@pytest.mark.asyncio
async def test_mistral_backend_fails_over_through_the_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import respx

    from vibe.core.config import ModelConfig, ProviderConfig
    from vibe.core.llm.backend.mistral import MistralBackend
    from vibe.core.types import Backend, LLMMessage, Role

    monkeypatch.setenv("VIBE_HOME", str(tmp_path))
    monkeypatch.setenv("POOL_TEST_API_KEY", "key-a")
    monkeypatch.setenv("POOL_TEST_API_KEYS", "key-a,key-b")
    provider = ProviderConfig(
        name="mistral",
        api_base="https://api.mistral.ai/v1",
        api_key_env_var="POOL_TEST_API_KEY",
        backend=Backend.MISTRAL,
    )
    model = ModelConfig(name="mistral-vibe-cli-latest", provider="mistral", alias="m")
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].removeprefix("Bearer ")
        seen.append(key)
        if key == "key-a":
            return httpx.Response(
                429, json={"message": "Rate limit exceeded"}, headers={"retry-after": "60"}
            )
        return httpx.Response(
            200,
            json={
                "id": "1",
                "object": "chat.completion",
                "created": 1,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": f"hello {key}"},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    with respx.mock(assert_all_called=False) as router:
        router.post("https://api.mistral.ai/v1/chat/completions").mock(side_effect=answer)
        async with MistralBackend(provider=provider) as backend:
            chunk = await backend.complete(
                model=model,
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            )

    assert chunk.message.content == "hello key-b"
    assert seen == ["key-a", "key-b"]
