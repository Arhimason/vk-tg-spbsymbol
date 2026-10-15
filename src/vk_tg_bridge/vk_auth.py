import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from vk_tg_bridge.constants import (
    DEFAULT_EXPIRES_IN_SECONDS,
    EXCHANGE_INITIATOR,
    EXCHANGE_SCOPE,
    GRANT_TYPE_EXCHANGE_TOKEN,
    GRANT_TYPE_REFRESH_TOKEN,
    TOKEN_REFRESH_SKEW_SECONDS,
    VK_MESSENGER_CLIENT_ID,
    JsonKey,
)
from vk_tg_bridge.models import ExchangeForm, RefreshForm, Settings, VkCredentials


class VkAuthError(Exception):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(message)


def resolve_credentials(
    saved: VkCredentials | None,
    settings: Settings,
    session: VkCredentials | None = None,
) -> VkCredentials:
    if saved is not None and saved.can_renew():
        return saved
    if session is not None and session.can_exchange():
        return session
    expires_at = 0.0 if _can_refresh_settings(settings) else None
    return VkCredentials(
        access_token=settings.vk_access_token,
        refresh_token=settings.vk_refresh_token,
        device_id=settings.vk_device_id,
        client_id=settings.vk_client_id,
        expires_at=expires_at,
    )


def should_refresh(credentials: VkCredentials, now: float) -> bool:
    if not credentials.can_renew() or credentials.expires_at is None:
        return False
    return now >= credentials.expires_at - TOKEN_REFRESH_SKEW_SECONDS


def build_refresh_form(credentials: VkCredentials) -> RefreshForm:
    if not credentials.can_refresh():
        raise VkAuthError(code=0, message="нет refresh_token, device_id и client_id")
    return RefreshForm(
        grant_type=GRANT_TYPE_REFRESH_TOKEN,
        refresh_token=credentials.refresh_token,
        client_id=credentials.client_id,
        device_id=credentials.device_id,
        state=str(uuid.uuid4()),
    )


def credentials_from_refresh(
    payload: dict[str, Any],
    *,
    previous: VkCredentials,
    expected_state: str,
    now: float,
) -> VkCredentials:
    error = payload.get(JsonKey.ERROR)
    if error:
        description = payload.get(JsonKey.ERROR_DESCRIPTION) or error
        raise VkAuthError(code=0, message=str(description))
    returned_state = payload.get(JsonKey.STATE)
    if returned_state not in (None, expected_state):
        raise VkAuthError(code=0, message="state ответа VK ID не совпал")
    access_token = payload.get(JsonKey.ACCESS_TOKEN)
    refresh_token = payload.get(JsonKey.REFRESH_TOKEN)
    if not isinstance(access_token, str) or not access_token:
        raise VkAuthError(code=0, message="в ответе VK ID нет access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise VkAuthError(code=0, message="в ответе VK ID нет refresh_token")
    expires_in = payload.get(JsonKey.EXPIRES_IN) or DEFAULT_EXPIRES_IN_SECONDS
    return VkCredentials(
        access_token=access_token,
        refresh_token=refresh_token,
        device_id=previous.device_id,
        client_id=previous.client_id,
        expires_at=now + int(expires_in),
    )


def build_exchange_form(credentials: VkCredentials) -> ExchangeForm:
    if not credentials.can_exchange():
        raise VkAuthError(code=0, message="нет exchange_token, device_id и client_id")
    return ExchangeForm(
        grant_type=GRANT_TYPE_EXCHANGE_TOKEN,
        exchange_token=credentials.exchange_token,
        client_id=credentials.client_id,
        device_id=credentials.device_id,
        scope=EXCHANGE_SCOPE,
        initiator=EXCHANGE_INITIATOR,
    )


def payload_from_exchange_response(*, status: int, location: str, body: str) -> dict[str, Any]:
    fragment = parse_qs(urlparse(location).fragment)
    if fragment.get(JsonKey.ACCESS_TOKEN) or fragment.get(JsonKey.ERROR):
        return {key: values[-1] for key, values in fragment.items() if values}
    if not body.strip():
        raise VkAuthError(code=status, message=f"обмен токена пустой, HTTP {status}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise VkAuthError(
            code=status,
            message=f"обмен токена вернул не JSON, HTTP {status}",
        ) from exc
    if not isinstance(payload, dict):
        raise VkAuthError(code=status, message="обмен токена вернул не объект")
    return payload


def credentials_from_exchange(
    payload: dict[str, Any],
    *,
    previous: VkCredentials,
    now: float,
) -> VkCredentials:
    body = _exchange_body(payload)
    error = _exchange_error(payload, body)
    if error:
        raise VkAuthError(code=0, message=error)
    access_token = _nested_token(body.get(JsonKey.ACCESS_TOKEN))
    if not access_token:
        raise VkAuthError(code=0, message="в ответе обмена нет access_token")
    exchange_token = _nested_token(body.get(JsonKey.EXCHANGE_TOKEN)) or previous.exchange_token
    expires_in = body.get(JsonKey.EXPIRES_IN)
    if expires_in in (None, ""):
        expires_in = DEFAULT_EXPIRES_IN_SECONDS
    expires_in = int(expires_in)
    expires_at = None if expires_in <= 0 else now + expires_in
    return VkCredentials(
        access_token=access_token,
        refresh_token=previous.refresh_token,
        exchange_token=exchange_token,
        device_id=previous.device_id,
        client_id=previous.client_id,
        expires_at=expires_at,
    )


def read_session(path: str) -> VkCredentials | None:
    file = Path(path)
    if not file.is_file():
        return None
    payload = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    access_token = payload.get(JsonKey.ACCESS_TOKEN)
    exchange_token = payload.get(JsonKey.EXCHANGE_TOKEN)
    device_id = payload.get(JsonKey.DEVICE_ID)
    if not all(isinstance(value, str) and value for value in (access_token, exchange_token, device_id)):
        return None
    client_id = payload.get(JsonKey.CLIENT_ID) or VK_MESSENGER_CLIENT_ID
    return VkCredentials(
        access_token=access_token,
        exchange_token=exchange_token,
        device_id=device_id,
        client_id=str(client_id),
        expires_at=_session_expiry(payload),
    )


def write_session(path: str, credentials: VkCredentials) -> None:
    file = Path(path)
    payload: dict[str, Any] = {}
    if file.is_file():
        loaded = json.loads(file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            payload = loaded
    payload[JsonKey.ACCESS_TOKEN] = credentials.access_token
    payload[JsonKey.EXCHANGE_TOKEN] = credentials.exchange_token
    payload[JsonKey.DEVICE_ID] = credentials.device_id
    payload[JsonKey.CLIENT_ID] = credentials.client_id
    if credentials.expires_at is not None:
        payload["expires_at"] = credentials.expires_at
        payload["expires_at_local"] = datetime.fromtimestamp(credentials.expires_at).strftime(
            "%Y-%m-%d %H:%M"
        )
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    file.chmod(0o600)


def _can_refresh_settings(settings: Settings) -> bool:
    return bool(
        settings.vk_refresh_token and settings.vk_device_id and settings.vk_client_id
    )


def _exchange_body(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get(JsonKey.RESPONSE)
    if isinstance(nested, dict) and _nested_token(nested.get(JsonKey.ACCESS_TOKEN)):
        return nested
    return payload


def _exchange_error(payload: dict[str, Any], body: dict[str, Any]) -> str:
    for source in (body, payload):
        error = source.get(JsonKey.ERROR)
        if isinstance(error, str) and error:
            description = source.get(JsonKey.ERROR_DESCRIPTION) or error
            return str(description)
        if isinstance(error, dict):
            description = error.get(JsonKey.ERROR) or error.get(JsonKey.ERROR_MSG) or error.get(JsonKey.ERROR_DESCRIPTION)
            if description:
                return str(description)
    return ""


def _nested_token(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        token = value.get("token") or value.get(JsonKey.ACCESS_TOKEN)
        if isinstance(token, str):
            return token
    return ""


def _session_expiry(payload: dict[str, Any]) -> float | None:
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, int | float) and expires_at > 0:
        return float(expires_at)
    created = payload.get("created")
    expires_in = payload.get(JsonKey.EXPIRES_IN)
    if created in (None, "") or expires_in in (None, ""):
        return None
    expires_in_seconds = int(expires_in)
    if expires_in_seconds <= 0:
        return None
    created_seconds = int(created) / 1000 if int(created) > 10_000_000_000 else int(created)
    return created_seconds + expires_in_seconds
