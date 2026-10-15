import json

import pytest

from vk_tg_bridge.models import Settings, VkCredentials
from vk_tg_bridge.vk_auth import (
    VkAuthError,
    build_exchange_form,
    build_refresh_form,
    credentials_from_exchange,
    payload_from_exchange_response,
    credentials_from_refresh,
    read_session,
    resolve_credentials,
    should_refresh,
    write_session,
)


def _settings(**overrides: str) -> Settings:
    data = {
        "vk_access_token": "access-from-env",
        "telegram_api_id": 1,
        "telegram_api_hash": "hash",
        "telegram_bot_token": "bot",
        "vk_refresh_token": "",
        "vk_device_id": "",
        "vk_client_id": "",
    }
    data.update(overrides)
    return Settings(**data)


def test_static_token_is_not_refreshed() -> None:
    credentials = resolve_credentials(saved=None, settings=_settings())
    assert credentials.access_token == "access-from-env"
    assert credentials.expires_at is None
    assert should_refresh(credentials, now=10_000) is False


def test_saved_rotating_token_wins_over_env() -> None:
    saved = VkCredentials(
        access_token="rotated",
        refresh_token="refresh-1",
        device_id="device",
        client_id="123",
        expires_at=5000,
    )
    resolved = resolve_credentials(
        saved=saved,
        settings=_settings(vk_refresh_token="env-refresh", vk_device_id="device", vk_client_id="123"),
    )
    assert resolved.access_token == "rotated"
    assert should_refresh(resolved, now=4800) is True
    assert should_refresh(resolved, now=1000) is False


def test_refresh_form_keeps_original_device_id() -> None:
    credentials = VkCredentials(
        access_token="access",
        refresh_token="refresh-1",
        device_id="device-original",
        client_id="123",
        expires_at=0,
    )
    form = build_refresh_form(credentials)
    assert form.grant_type == "refresh_token"
    assert form.device_id == "device-original"
    assert form.refresh_token == "refresh-1"
    updated = credentials_from_refresh(
        {
            "access_token": "access-2",
            "refresh_token": "refresh-2",
            "expires_in": 3600,
            "state": form.state,
            "device_id": "do-not-use",
        },
        previous=credentials,
        expected_state=form.state,
        now=100,
    )
    assert updated.access_token == "access-2"
    assert updated.refresh_token == "refresh-2"
    assert updated.device_id == "device-original"
    assert updated.expires_at == 3700


def test_refresh_rejects_mismatched_state() -> None:
    credentials = VkCredentials(
        access_token="access",
        refresh_token="refresh-1",
        device_id="device",
        client_id="123",
    )
    with pytest.raises(VkAuthError):
        credentials_from_refresh(
            {
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "state": "other",
            },
            previous=credentials,
            expected_state="expected",
            now=1,
        )


def test_refresh_without_device_is_refused() -> None:
    credentials = VkCredentials(access_token="access", refresh_token="refresh-1")
    with pytest.raises(VkAuthError):
        build_refresh_form(credentials)


def test_exchange_form_uses_messenger_client_and_keeps_device() -> None:
    credentials = VkCredentials(
        access_token="access-1",
        exchange_token="exchange-1",
        device_id="device-original",
        client_id="51453752",
        expires_at=1_000,
    )
    form = build_exchange_form(credentials)
    assert form.grant_type == "exchange_token"
    assert form.client_id == "51453752"
    assert form.device_id == "device-original"
    assert form.initiator == "expired_token"
    updated = credentials_from_exchange(
        {
            "access_token": "access-2",
            "exchange_token": "exchange-2",
            "expires_in": 86400,
            "user_id": 1,
        },
        previous=credentials,
        now=100,
    )
    assert updated.access_token == "access-2"
    assert updated.exchange_token == "exchange-2"
    assert updated.device_id == "device-original"
    assert updated.client_id == "51453752"
    assert updated.expires_at == 86500
    assert should_refresh(updated, now=86_200) is True


def test_exchange_reads_oauth_redirect_fragment() -> None:
    credentials = VkCredentials(
        access_token="access-1",
        exchange_token="exchange-1",
        device_id="device",
        client_id="51453752",
    )
    payload = payload_from_exchange_response(
        status=302,
        location=(
            "https://oauth.vk.com/blank.html#access_token=access-2"
            "&expires_in=86400&user_id=1"
        ),
        body="<!DOCTYPE html>",
    )
    updated = credentials_from_exchange(payload, previous=credentials, now=100)
    assert updated.access_token == "access-2"
    assert updated.exchange_token == "exchange-1"
    assert updated.expires_at == 86500


def test_exchange_reads_nested_token_and_keeps_previous_exchange() -> None:
    credentials = VkCredentials(
        access_token="access-1",
        exchange_token="exchange-1",
        device_id="device",
        client_id="51453752",
    )
    updated = credentials_from_exchange(
        {"response": {"access_token": {"token": "access-2"}, "expires_in": 0}},
        previous=credentials,
        now=50,
    )
    assert updated.access_token == "access-2"
    assert updated.exchange_token == "exchange-1"
    assert updated.expires_at is None


def test_session_file_roundtrip(tmp_path) -> None:
    path = tmp_path / "vk-session.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "access-1",
                "exchange_token": "exchange-1",
                "device_id": "device",
                "created": 1_792_070_129_803,
                "expires_in": 86400,
            }
        ),
        encoding="utf-8",
    )
    loaded = read_session(str(path))
    assert loaded is not None
    assert loaded.client_id == "51453752"
    assert loaded.can_exchange()
    resolved = resolve_credentials(saved=None, settings=_settings(), session=loaded)
    assert resolved.exchange_token == "exchange-1"
    write_session(str(path), loaded.model_copy(update={"access_token": "access-2"}))
    assert read_session(str(path)).access_token == "access-2"
