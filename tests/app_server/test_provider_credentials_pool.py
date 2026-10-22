"""``ProviderCredentialService`` with several pooled accounts (``<VAR>S``)."""

from __future__ import annotations

import pytest

from tests.app_server.test_provider_credentials import _config, _orchestrator, _provider

pytest.importorskip("mistralai_vibe_local_harness.vibe")

from mistralai_vibe_local_harness.vibe import (
    ProviderAuthRequired,
    ProviderCredentialSnapshot,
)
from vibe.app_server._provider_credentials import ProviderCredentialService


def _service() -> ProviderCredentialService:
    return ProviderCredentialService(_orchestrator(_config(_provider("primary"))))


async def _token(service: ProviderCredentialService) -> tuple[str, str]:
    result = await service.resolve()
    assert isinstance(result, ProviderCredentialSnapshot)
    assert result.token is not None
    return result.token, result.revision


@pytest.mark.asyncio
async def test_resolutions_spread_over_the_pooled_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRIMARY_KEY", raising=False)
    monkeypatch.setenv("PRIMARY_KEYS", "key-a,key-b")
    service = _service()

    first, _ = await _token(service)
    second, _ = await _token(service)
    third, _ = await _token(service)

    assert {first, second} == {"key-a", "key-b"}
    assert third == first


@pytest.mark.asyncio
async def test_a_rejected_account_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRIMARY_KEY", raising=False)
    monkeypatch.setenv("PRIMARY_KEYS", "key-a,key-b")
    service = _service()
    refused, revision = await _token(service)

    await service.reject(observed_revision=revision, reason="http_unauthorized")

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
