from __future__ import annotations

import pytest

from vibe.utils import api_keys


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    store: dict[str, str] = {}
    monkeypatch.setattr(
        api_keys, "get_api_key_from_keyring", lambda name, **_: store.get(name)
    )
    monkeypatch.setattr(
        api_keys,
        "set_api_key_in_keyring",
        lambda name, value: store.update({name: value}),
    )
    monkeypatch.delenv("VIBE_API_KEY", raising=False)
    monkeypatch.delenv("VIBE_API_KEYS", raising=False)
    return store


def test_each_sign_in_adds_an_account_without_duplicates(
    fake_keyring: dict[str, str],
) -> None:
    for key in ("mstrl_a", "mstrl_b", "mstrl_a"):
        assert api_keys.add_pooled_key("VIBE_API_KEY", key)

    assert fake_keyring["VIBE_API_KEYS"] == "mstrl_a,mstrl_b"
    assert api_keys.pooled_keys("VIBE_API_KEY") == ["mstrl_a", "mstrl_b"]


def test_saved_accounts_stand_in_for_a_missing_single_key(
    fake_keyring: dict[str, str],
) -> None:
    fake_keyring["VIBE_API_KEYS"] = "mstrl_a,mstrl_b"

    assert api_keys.resolve_api_key("VIBE_API_KEY") == "mstrl_a"


def test_environment_accounts_come_before_saved_ones(
    fake_keyring: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_keyring["VIBE_API_KEYS"] = "mstrl_b"
    monkeypatch.setenv("VIBE_API_KEYS", "mstrl_a, mstrl_b")

    assert api_keys.pooled_keys("VIBE_API_KEY") == ["mstrl_a", "mstrl_b"]
