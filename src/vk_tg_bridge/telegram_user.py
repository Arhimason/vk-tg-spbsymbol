import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events, utils
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAboutNotModifiedError,
    ChatNotModifiedError,
    FloodWaitError,
    PeerIdInvalidError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.account import UpdateNotifySettingsRequest
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    EditPhotoRequest,
    EditTitleRequest,
    InviteToChannelRequest,
)
from telethon.tl.functions.messages import (
    EditChatAboutRequest,
    SendReactionRequest,
    SetChatAvailableReactionsRequest,
    UpdatePinnedMessageRequest,
)
from telethon.tl.types import (
    Channel,
    ChatReactionsAll,
    InputChatUploadedPhoto,
    InputPeerNotifySettings,
    ReactionEmoji,
)

from vk_tg_bridge.constants import (
    ATTACHMENT_FAILED_NOTE,
    DELIVERED_REACTION,
    GROUP_ABOUT,
    SERVICE_CHAT_ABOUT,
    SERVICE_CHAT_TITLE,
    MEDIA_DOCUMENT,
    MEDIA_PHOTO,
    MEDIA_VIDEO,
    MEDIA_VIDEO_NOTE,
    MEDIA_VOICE,
    RELAY_CHAT_MEMBERS,
    VK_FILE_LIMIT_BYTES,
    VK_PHOTO_LIMIT_BYTES,
    VK_VOICE_LIMIT_BYTES,
)
from vk_tg_bridge.models import OutboundFile, Settings

logger = logging.getLogger(__name__)

ReplyHandler = Callable[..., Awaitable[None]]


class TelegramUser:
    """Личная сессия Telegram через Telethon, без прокси."""

    def __init__(self, *, settings: Settings, bot_username: str) -> None:
        session = Path(settings.telegram_session_path)
        session.parent.mkdir(parents=True, exist_ok=True)
        self._bot_username = bot_username
        self._client = TelegramClient(
            session=str(session),
            api_id=settings.telegram_api_id,
            api_hash=settings.telegram_api_hash,
        )
        self._relay_members = RELAY_CHAT_MEMBERS
        self._bot_id: int | None = None
        self._on_reply: ReplyHandler | None = None
        self._on_edit: ReplyHandler | None = None
        self._on_delete: ReplyHandler | None = None
        self._reactions_ready: set[int] = set()

    async def connect(self) -> None:
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise SystemExit("Нет сессии Telegram. Сначала: vk-tg-bridge login")
        if self._relay_members and self._bot_username:
            self._bot_id = await self._remember_bot()
        own_only = None if self._relay_members else True
        self._client.add_event_handler(
            self._on_outgoing,
            events.NewMessage(outgoing=own_only),
        )
        self._client.add_event_handler(
            self._on_edited,
            events.MessageEdited(outgoing=own_only),
        )
        self._client.add_event_handler(self._on_deleted, events.MessageDeleted())
        if self._relay_members:
            logger.info("сообщения участников чата, кроме бота, уходят в VK")
        logger.info("личная сессия Telethon подключена")

    async def disconnect(self) -> None:
        await self._client.disconnect()

    def set_reply_handler(self, handler: ReplyHandler) -> None:
        self._on_reply = handler

    def set_edit_handler(self, handler: ReplyHandler) -> None:
        self._on_edit = handler

    def set_delete_handler(self, handler: ReplyHandler) -> None:
        self._on_delete = handler

    async def create_group(self, title: str, photo: bytes | None = None) -> int:
        try:
            created = await self._client(
                CreateChannelRequest(
                    title=title,
                    about=GROUP_ABOUT,
                    megagroup=True,
                )
            )
        except FloodWaitError as exc:
            logger.error("Telegram просит подождать %s с перед новой группой", exc.seconds)
            raise
        channel = next(
            (chat for chat in created.chats if isinstance(chat, Channel)),
            None,
        )
        if not isinstance(channel, Channel):
            raise RuntimeError("Telegram не вернул созданную группу")
        chat_id = utils.get_peer_id(channel)
        if photo:
            await self._set_photo(channel=channel, photo=photo, title=title)
        await self.allow_reactions(chat_id)
        logger.info("создана группа %s (%s)", title, chat_id)
        return chat_id

    async def has_chat(self, chat_id: int) -> bool:
        try:
            await self._client.get_entity(chat_id)
        except FloodWaitError:
            raise
        except (ChannelInvalidError, ChannelPrivateError, PeerIdInvalidError, ValueError):
            return False
        except Exception:
            logger.exception("не удалось проверить чат %s", chat_id)
            return False
        return True

    async def open_service_chat(self) -> int:
        try:
            created = await self._client(
                CreateChannelRequest(
                    title=SERVICE_CHAT_TITLE,
                    about=SERVICE_CHAT_ABOUT,
                    megagroup=True,
                )
            )
        except FloodWaitError as exc:
            logger.error("Telegram просит подождать %s с перед служебным чатом", exc.seconds)
            raise
        channel = next(
            (chat for chat in created.chats if isinstance(chat, Channel)),
            None,
        )
        if not isinstance(channel, Channel):
            raise RuntimeError("Telegram не вернул служебный чат")
        chat_id = utils.get_peer_id(channel)
        await self.invite_bot(chat_id)
        await self._mute(chat_id)
        logger.info("служебный чат %s (%s)", SERVICE_CHAT_TITLE, chat_id)
        return chat_id

    async def pin(self, chat_id: int, message_id: int) -> None:
        try:
            await self._client(
                UpdatePinnedMessageRequest(peer=chat_id, id=message_id, silent=True)
            )
        except Exception:
            logger.exception("не удалось закрепить статус в %s", chat_id)

    async def _mute(self, chat_id: int) -> None:
        try:
            await self._client(
                UpdateNotifySettingsRequest(
                    peer=chat_id,
                    settings=InputPeerNotifySettings(
                        silent=True,
                        mute_until=datetime.now() + timedelta(days=3650),
                    ),
                )
            )
        except Exception:
            logger.exception("не удалось заглушить чат %s", chat_id)

    async def allow_reactions(self, chat_id: int) -> None:
        if chat_id in self._reactions_ready:
            return
        try:
            await self._client(
                SetChatAvailableReactionsRequest(
                    peer=chat_id,
                    available_reactions=ChatReactionsAll(),
                )
            )
        except ChatNotModifiedError:
            logger.info("в чате %s уже разрешены все реакции", chat_id)
        except Exception:
            logger.exception("не удалось разрешить реакции в чате %s", chat_id)
            return
        self._reactions_ready.add(chat_id)

    async def react(
        self,
        *,
        chat_id: int,
        message_id: int,
        emoji: str = DELIVERED_REACTION,
    ) -> None:
        await self._client(
            SendReactionRequest(
                peer=chat_id,
                msg_id=message_id,
                reaction=[ReactionEmoji(emoticon=emoji)],
            )
        )

    async def _set_photo(self, *, channel: Channel | int, photo: bytes, title: str) -> None:
        try:
            uploaded = await self._client.upload_file(photo, file_name="avatar.jpg")
            await self._client(
                EditPhotoRequest(
                    channel=channel,
                    photo=InputChatUploadedPhoto(file=uploaded),
                )
            )
        except Exception:
            logger.exception("не удалось поставить аватар группы %s", title)

    async def set_about(self, chat_id: int, about: str) -> None:
        try:
            await self._client(EditChatAboutRequest(peer=chat_id, about=about))
        except (ChatNotModifiedError, ChatAboutNotModifiedError):
            logger.info("описание чата %s уже совпадает", chat_id)
        except FloodWaitError as exc:
            logger.error("Telegram просит подождать %s с перед описанием чата", exc.seconds)
            raise

    async def refresh_group(self, chat_id: int, *, title: str, photo: bytes | None) -> None:
        try:
            await self._client(EditTitleRequest(channel=chat_id, title=title))
        except ChatNotModifiedError:
            logger.info("название группы %s уже совпадает", chat_id)
        except FloodWaitError:
            raise
        except Exception:
            logger.exception("не удалось переименовать группу %s", chat_id)
            return
        if photo:
            await self._set_photo(channel=chat_id, photo=photo, title=title)

    async def invite_bot(self, chat_id: int) -> None:
        channel = await self._client.get_entity(chat_id)
        bot = await self._client.get_input_entity(self._bot_username)
        try:
            await self._client(InviteToChannelRequest(channel=channel, users=[bot]))
        except UserAlreadyParticipantError:
            logger.info("бот уже состоит в чате %s", chat_id)
            return
        logger.info("бот приглашён в чат %s", chat_id)

    async def login(self) -> None:
        await self._client.start()
        await self._client.disconnect()
        logger.info("сессия Telegram сохранена")

    async def _remember_bot(self) -> int | None:
        try:
            entity = await self._client.get_entity(self._bot_username)
        except Exception:
            logger.exception("не удалось узнать id бота %s", self._bot_username)
            return None
        bot_id = getattr(entity, "id", None)
        if not isinstance(bot_id, int):
            logger.error("не удалось узнать id бота %s", self._bot_username)
            return None
        return bot_id

    def _accepts(self, event: Any) -> bool:
        message = getattr(event, "message", None)
        service = message is not None and getattr(message, "action", None) is not None
        sender_id = getattr(event, "sender_id", None)
        return counts_as_own_message(
            relay_members=self._relay_members,
            outgoing=bool(getattr(event, "out", False)),
            sender_id=sender_id if isinstance(sender_id, int) else None,
            bot_id=self._bot_id,
            service=service,
        )

    async def _on_outgoing(self, event: events.NewMessage.Event) -> None:
        if event.chat_id is None or self._on_reply is None or not self._accepts(event):
            return
        if self._relay_members and not event.out:
            logger.info(
                "сообщение участника %s в чате %s уходит в VK",
                event.sender_id,
                event.chat_id,
            )
        try:
            text, files = await outgoing_parts(event)
            await self._on_reply(
                chat_id=event.chat_id,
                text=text,
                files=files,
                telegram_message_id=telegram_message_id(event),
                reply_to_message_id=telegram_reply_target(event),
            )
        except Exception:
            logger.exception("не удалось отправить ответ в VK")

    async def _on_edited(self, event: events.MessageEdited.Event) -> None:
        if event.chat_id is None or self._on_edit is None or not self._accepts(event):
            return
        try:
            await self._on_edit(
                chat_id=event.chat_id,
                telegram_message_id=telegram_message_id(event),
                text=_outgoing_text(event),
            )
        except Exception:
            logger.exception("не удалось передать правку в VK")

    async def _on_deleted(self, event: events.MessageDeleted.Event) -> None:
        if event.chat_id is None or self._on_delete is None:
            return
        for message_id in event.deleted_ids:
            try:
                await self._on_delete(chat_id=event.chat_id, telegram_message_id=message_id)
            except Exception:
                logger.exception("не удалось передать удаление %s в VK", message_id)


def counts_as_own_message(
    *,
    relay_members: bool,
    outgoing: bool,
    sender_id: int | None,
    bot_id: int | None,
    service: bool,
) -> bool:
    if service:
        return False
    if not relay_members:
        return outgoing
    if outgoing:
        return True
    if sender_id is None or bot_id is None or sender_id == bot_id:
        return False
    return True


def telegram_message_id(event: Any) -> int | None:
    message = getattr(event, "message", None)
    value = getattr(message, "id", None)
    if isinstance(value, int):
        return value
    return None


def telegram_reply_target(event: Any) -> int | None:
    header = getattr(event, "reply_to", None)
    target = getattr(header, "reply_to_msg_id", None)
    if isinstance(target, int):
        return target
    return None


async def outgoing_parts(event: Any) -> tuple[str, list[OutboundFile]]:
    text = _outgoing_text(event)
    downloaded = await _outgoing_file(event)
    if downloaded is None and _has_media(event):
        text = "\n".join(part for part in (text, ATTACHMENT_FAILED_NOTE) if part)
        return text, []
    files = [downloaded] if downloaded is not None else []
    return text, files


async def _outgoing_file(event: Any) -> OutboundFile | None:
    if getattr(event, "photo", None):
        return await _file(event, kind=MEDIA_PHOTO, filename="photo.jpg", limit=VK_PHOTO_LIMIT_BYTES, label="фото")
    if getattr(event, "voice", None):
        return await _file(event, kind=MEDIA_VOICE, filename="voice.ogg", limit=VK_VOICE_LIMIT_BYTES, label="голосовое")
    if getattr(event, "sticker", None):
        return await _file(
            event,
            kind=MEDIA_DOCUMENT,
            filename=_sticker_name(event),
            limit=VK_FILE_LIMIT_BYTES,
            label="стикер",
        )
    if getattr(event, "gif", None):
        return await _file(
            event,
            kind=MEDIA_VIDEO,
            filename="animation.mp4",
            limit=VK_FILE_LIMIT_BYTES,
            label="анимация",
        )
    if getattr(event, "video_note", None):
        return await _file(
            event,
            kind=MEDIA_VIDEO_NOTE,
            filename="video_note.mp4",
            limit=VK_FILE_LIMIT_BYTES,
            label="видеосообщение",
        )
    if getattr(event, "video", None):
        return await _file(
            event,
            kind=MEDIA_VIDEO,
            filename=_file_name(event, "video.mp4"),
            limit=VK_FILE_LIMIT_BYTES,
            label="видео",
        )
    if getattr(event, "audio", None):
        return await _file(
            event,
            kind=MEDIA_DOCUMENT,
            filename=_file_name(event, "audio.mp3"),
            limit=VK_FILE_LIMIT_BYTES,
            label="аудио",
        )
    if getattr(event, "document", None):
        kind = MEDIA_PHOTO if _mime(event).startswith("image/") and _mime(event) not in {"image/gif", "image/webp"} else MEDIA_DOCUMENT
        return await _file(
            event,
            kind=kind,
            filename=_file_name(event, "photo.jpg" if kind == MEDIA_PHOTO else "file.bin"),
            limit=VK_PHOTO_LIMIT_BYTES if kind == MEDIA_PHOTO else VK_FILE_LIMIT_BYTES,
            label="файл",
        )
    return None


async def _file(
    event: Any,
    *,
    kind: str,
    filename: str,
    limit: int,
    label: str,
) -> OutboundFile | None:
    content = await _download_limited(event, limit=limit, kind=label)
    if content is None:
        return None
    return OutboundFile(kind=kind, filename=filename, content=content)


def _outgoing_text(event: Any) -> str:
    parts: list[str] = []
    forwarded = _forward_label(event)
    if forwarded:
        parts.append(forwarded)
    raw = getattr(event, "raw_text", None) or ""
    if isinstance(raw, str) and raw.strip():
        parts.append(raw.strip())
    special = _special_text(event)
    if special:
        parts.append(special)
    return "\n".join(parts)


def _forward_label(event: Any) -> str:
    header = getattr(event, "fwd_from", None)
    if header is None:
        return ""
    name = getattr(header, "from_name", None) or getattr(header, "post_author", None) or ""
    if isinstance(name, str) and name.strip():
        return f"Переслано от {name.strip()}"
    return "Пересланное сообщение"


def _special_text(event: Any) -> str:
    contact = getattr(event, "contact", None)
    if contact is not None:
        first = getattr(contact, "first_name", "") or ""
        last = getattr(contact, "last_name", "") or ""
        phone = getattr(contact, "phone_number", "") or ""
        name = f"{first} {last}".strip()
        line = " ".join(part for part in (name, str(phone) if phone else "") if part)
        return f"Контакт: {line}" if line else "Контакт"
    venue = getattr(event, "venue", None)
    if venue is not None:
        title = getattr(venue, "title", "") or ""
        address = getattr(venue, "address", "") or ""
        line = ", ".join(part for part in (title, address) if part)
        return f"Место: {line}" if line else "Место"
    geo = getattr(event, "geo", None)
    if geo is not None:
        lat = getattr(geo, "lat", None)
        lon = getattr(geo, "long", None)
        if lat is not None and lon is not None:
            return f"Геопозиция: {lat}, {lon}"
    poll = getattr(event, "poll", None)
    if poll is not None:
        question = getattr(poll, "question", None)
        if question is None:
            inner = getattr(poll, "poll", None)
            question = getattr(inner, "question", None) if inner is not None else None
        return f"Опрос: {question}" if question else "Опрос"
    dice = getattr(event, "dice", None)
    if dice is not None:
        emoticon = getattr(dice, "emoticon", "") or ""
        value = getattr(dice, "value", "")
        line = f"{emoticon} {value}".strip()
        return line or "Игральная кость"
    return ""


def _has_media(event: Any) -> bool:
    names = ("photo", "voice", "sticker", "gif", "video_note", "video", "audio", "document")
    return any(getattr(event, name, None) for name in names)


def _sticker_name(event: Any) -> str:
    mime = _mime(event)
    if "webm" in mime:
        return "sticker.webm"
    if "tgsticker" in mime:
        return "sticker.tgs"
    return "sticker.webp"


def _mime(event: Any) -> str:
    file = getattr(event, "file", None)
    mime = getattr(file, "mime_type", None) if file is not None else None
    return mime if isinstance(mime, str) else ""


def _file_name(event: Any, default: str) -> str:
    file = getattr(event, "file", None)
    name = getattr(file, "name", None) if file is not None else None
    if not isinstance(name, str) or not name.strip():
        return default
    cleaned = name.replace("\\", "_").replace("/", "_").strip()
    return cleaned[:120] or default


async def _download_limited(event: Any, *, limit: int, kind: str) -> bytes | None:
    data = await event.download_media(file=bytes)
    if not isinstance(data, bytes | bytearray) or not data:
        logger.error("Telegram не отдал %s", kind)
        return None
    if len(data) > limit:
        logger.error("%s больше лимита VK: %s байт", kind, len(data))
        return None
    return bytes(data)
