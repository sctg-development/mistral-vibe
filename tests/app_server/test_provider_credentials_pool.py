"""``ProviderCredentialService`` with several pooled accounts (``<VAR>S``)."""

from __future__ import annotations

from typing import Literal

import pytest

from tests.app_server.test_provider_credentials import _config, _orchestrator, _provider

pytest.importorskip("mistralai_vibe_local_harness.vibe")

from mistralai_vibe_local_harness.vibe import (
    ProviderAuthRequired,
    ProviderCredentialSnapshot,
)
from vibe.app_server._provider_credentials import ProviderCredentialService
from vibe.core.platform_auth import (
    ApiKeyPlatformCredentialProvider,
    PlatformCredentialSnapshot,
    PlatformCredentialTarget,
)
from vibe.core.platform_auth._mistral import api_key_credentials
from vibe.core.types import Backend


def _service() -> ProviderCredentialService:
    return ProviderCredentialService(_orchestrator(_config(_provider("primary"))))


async def _token(service: ProviderCredentialService) -> tuple[str, str]:
    result = await service.resolve()
    assert isinstance(result, ProviderCredentialSnapshot)
    assert result.token is not None
    return result.token, result.revision


@pytest.mark.asyncio
async def test_resolutions_keep_the_same_account_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRIMARY_KEY", raising=False)
    monkeypatch.delenv("VIBE_POOL_AGRESSIVE_ROTATION", raising=False)
    monkeypatch.setenv("PRIMARY_KEYS", "key-a,key-b")
    service = _service()

    first, _ = await _token(service)
    second, _ = await _token(service)
    third, _ = await _token(service)

    assert first == second == third


@pytest.mark.asyncio
async def test_first_party_pool_is_used_for_model_and_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_API_KEY", "key-primary")
    monkeypatch.setenv("VIBE_API_KEYS", "key-primary,key-b")
    monkeypatch.setenv("VIBE_POOL_STATIC", "1")
    monkeypatch.setenv("VIBE_POOL_AGRESSIVE_ROTATION", "1")
    provider = _provider(
        "mistral", api_key_env_var="VIBE_API_KEY", backend=Backend.MISTRAL
    )
    service = ProviderCredentialService(
        _orchestrator(_config(provider)), platform_credentials=api_key_credentials
    )

    account = service.account_credentials("VIBE_API_KEY")
    first = await account.resolve(PlatformCredentialTarget("https://mistral.example"))
    assert isinstance(first, PlatformCredentialSnapshot)
    assert first.headers["Authorization"] == "Bearer key-primary"
    assert (await _token(service))[0] == "key-primary"
    token, revision = await _token(service)
    assert token == "key-b"
    current = await service.account_credentials("VIBE_API_KEY").resolve(
        PlatformCredentialTarget("https://mistral.example")
    )
    assert isinstance(current, PlatformCredentialSnapshot)
    assert current.headers["Authorization"] == "Bearer key-b"
    await service.reject(observed_revision=revision, reason="http_unauthorized")
    current = await service.account_credentials("VIBE_API_KEY").resolve(
        PlatformCredentialTarget("https://mistral.example")
    )
    assert isinstance(current, PlatformCredentialSnapshot)
    assert current.headers["Authorization"] == "Bearer key-primary"


@pytest.mark.asyncio
async def test_fixed_platform_credential_never_uses_process_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_API_KEYS", "key-a,key-b")
    provider = _provider(
        "mistral", api_key_env_var="VIBE_API_KEY", backend=Backend.MISTRAL
    )
    platform = ApiKeyPlatformCredentialProvider.from_api_key("connection-key")
    service = ProviderCredentialService(
        _orchestrator(_config(provider)), platform_credentials=lambda _: platform
    )
    assert (await _token(service))[0] == "connection-key"
    assert service.account_credentials("VIBE_API_KEY") is platform


@pytest.mark.asyncio
async def test_pool_is_shuffled_only_once_per_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRIMARY_KEY", raising=False)
    monkeypatch.delenv("VIBE_POOL_STATIC", raising=False)
    monkeypatch.delenv("VIBE_POOL_AGRESSIVE_ROTATION", raising=False)
    monkeypatch.setenv("PRIMARY_KEYS", "key-a,key-b")
    shuffles: list[list[str]] = []

    def reverse(keys: list[str]) -> None:
        shuffles.append(keys.copy())
        keys.reverse()

    monkeypatch.setattr("vibe.core.llm.key_pool.random.shuffle", reverse)
    service = _service()
    assert (await _token(service))[0] == "key-b"
    assert (await _token(service))[0] == "key-b"
    assert shuffles == [["key-a", "key-b"]]


@pytest.mark.asyncio
async def test_aggressive_resolutions_spread_over_the_pooled_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRIMARY_KEY", raising=False)
    monkeypatch.setenv("PRIMARY_KEYS", "key-a,key-b")
    monkeypatch.setenv("VIBE_POOL_STATIC", "1")
    monkeypatch.setenv("VIBE_POOL_AGRESSIVE_ROTATION", "0")
    service = _service()

    first, _ = await _token(service)
    second, _ = await _token(service)
    third, _ = await _token(service)

    assert (first, second, third) == ("key-a", "key-b", "key-a")


@pytest.mark.asyncio
async def test_a_forbidden_account_is_not_parked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRIMARY_KEY", raising=False)
    monkeypatch.delenv("VIBE_POOL_AGRESSIVE_ROTATION", raising=False)
    monkeypatch.setenv("PRIMARY_KEYS", "key-a,key-b")
    service = _service()
    kept, revision = await _token(service)

    await service.reject(observed_revision=revision, reason="http_forbidden")

    for _ in range(3):
        token, _ = await _token(service)
        assert token == kept


@pytest.mark.asyncio
async def test_a_rejected_account_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason: Literal["http_unauthorized"] = "http_unauthorized"
    monkeypatch.delenv("PRIMARY_KEY", raising=False)
    monkeypatch.delenv("VIBE_POOL_AGRESSIVE_ROTATION", raising=False)
    monkeypatch.setenv("PRIMARY_KEYS", "key-a,key-b")
    service = _service()
    refused, revision = await _token(service)

    await service.reject(observed_revision=revision, reason=reason)

    for _ in range(3):
        token, _ = await _token(service)
        assert token != refused


@pytest.mark.asyncio
async def test_a_single_key_keeps_the_rejected_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIMARY_KEY", "only-key")
    monkeypatch.delenv("PRIMARY_KEYS", raising=False)
    service = _service()
    _, revision = await _token(service)

    await service.reject(observed_revision=revision, reason="http_unauthorized")
    result = await service.resolve()

    assert isinstance(result, ProviderAuthRequired)
    assert result.reason == "rejected"
