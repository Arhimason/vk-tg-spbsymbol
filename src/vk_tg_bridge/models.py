from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vk_tg_bridge.constants import JsonKey


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    vk_access_token: str = Field(repr=False)
    telegram_api_id: int
    telegram_api_hash: str = Field(repr=False)
    telegram_bot_token: str = Field(repr=False)
    vk_refresh_token: str = Field(default="", repr=False)
    vk_device_id: str = ""
    vk_client_id: str = ""
    telegram_session_path: str = "data/telegram.session"
    database_path: str = "data/bridge.db"


class VkCredentials(BaseModel):
    access_token: str = Field(repr=False)
    refresh_token: str = Field(default="", repr=False)
    exchange_token: str = Field(default="", repr=False)
    device_id: str = ""
    client_id: str = ""
    expires_at: float | None = None

    def can_refresh(self) -> bool:
        return bool(self.refresh_token and self.device_id and self.client_id)

    def can_exchange(self) -> bool:
        return bool(self.exchange_token and self.device_id and self.client_id)

    def can_renew(self) -> bool:
        return self.can_refresh() or self.can_exchange()


class RefreshForm(BaseModel):
    grant_type: str
    refresh_token: str = Field(repr=False)
    client_id: str
    device_id: str
    state: str


class ExchangeForm(BaseModel):
    grant_type: str
    exchange_token: str = Field(repr=False)
    client_id: str
    device_id: str
    scope: str
    initiator: str


class PeerLink(BaseModel):
    vk_peer_id: int
    title: str
    telegram_chat_id: int
    bot_invited: bool


class MessageLink(BaseModel):
    telegram_chat_id: int
    telegram_message_id: int
    vk_peer_id: int
    vk_message_id: int
    vk_conversation_message_id: int
    outgoing: bool = False
    body: str = ""


class IncomingVkMessage(BaseModel):
    message_id: int
    peer_id: int
    text: str


class InboundFile(BaseModel):
    kind: str
    url: str
    filename: str


class OutboundFile(BaseModel):
    kind: str
    filename: str
    content: bytes


class LoadedMessage(BaseModel):
    text: str
    files: list[InboundFile] = Field(default_factory=list)
    from_id: int | None = None
    conversation_message_id: int | None = None
    reply_conversation_message_id: int | None = None


class SentVkMessage(BaseModel):
    message_id: int
    conversation_message_id: int | None = None


class LongPollServer(BaseModel):
    server: str
    key: str = Field(repr=False)
    ts: int


class LongPollTick(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ts: int | None = None
    updates: list[Any] = Field(default_factory=list)
    failed: int | None = None

    @field_validator("ts", mode="before")
    @classmethod
    def coerce_ts(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        return int(value)

    @field_validator("failed", mode="before")
    @classmethod
    def coerce_failed(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        return int(value)


def tick_from_payload(payload: dict[str, Any]) -> LongPollTick:
    updates = payload.get(JsonKey.UPDATES, [])
    if not isinstance(updates, list):
        updates = []
    return LongPollTick(
        ts=payload.get(JsonKey.TS),
        updates=updates,
        failed=payload.get(JsonKey.FAILED),
    )
