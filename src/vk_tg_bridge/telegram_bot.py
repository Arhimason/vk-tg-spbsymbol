import logging
from typing import Any

import aiohttp

from vk_tg_bridge.avatar import animated_sticker_gif, sticker_on_white
from vk_tg_bridge.constants import (
    FILE_TOO_LARGE_NOTE,
    STICKER_FILE_NAME,
    MAX_PHOTOS_PER_MESSAGE,
    MEDIA_ANIMATION,
    MEDIA_AUDIO,
    MEDIA_DOCUMENT,
    MEDIA_PHOTO,
    MEDIA_VIDEO,
    MEDIA_VIDEO_NOTE,
    MEDIA_VOICE,
    TELEGRAM_API_URL,
    TELEGRAM_FILE_LIMIT_BYTES,
    TELEGRAM_EDIT_CAPTION,
    TELEGRAM_EDIT_MESSAGE,
    TELEGRAM_GET_ME,
    TELEGRAM_PHOTO_LIMIT_BYTES,
    TELEGRAM_SEND_ANIMATION,
    TELEGRAM_SEND_CHAT_ACTION,
    TELEGRAM_SEND_AUDIO,
    TELEGRAM_SEND_DOCUMENT,
    TELEGRAM_SEND_MESSAGE,
    TELEGRAM_SEND_PHOTO,
    TELEGRAM_SEND_VIDEO,
    TELEGRAM_SEND_VIDEO_NOTE,
    TELEGRAM_SEND_VOICE,
    TELEGRAM_TYPING_ACTION,
    JsonKey,
)
from vk_tg_bridge.models import InboundFile

logger = logging.getLogger(__name__)


class TelegramBotError(Exception):
    pass


class TelegramBot:
    def __init__(self, *, session: aiohttp.ClientSession, token: str) -> None:
        self._session = session
        self._token = token

    async def username(self) -> str:
        payload = await self._call(method=TELEGRAM_GET_ME, body=None)
        username = payload.get(JsonKey.USERNAME)
        if not isinstance(username, str) or not username:
            raise TelegramBotError("у бота нет username")
        return username

    async def send_text(self, *, chat_id: int, text: str) -> int:
        result = await self._call(
            method=TELEGRAM_SEND_MESSAGE,
            body={"chat_id": chat_id, "text": text},
        )
        message_id = _message_id(result)
        if message_id is None:
            raise TelegramBotError("Telegram не вернул id статусного сообщения")
        return message_id

    async def show_typing(self, chat_id: int) -> None:
        await self._call(
            method=TELEGRAM_SEND_CHAT_ACTION,
            body={"chat_id": chat_id, "action": TELEGRAM_TYPING_ACTION},
        )

    async def revise(self, *, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self._call(
                method=TELEGRAM_EDIT_MESSAGE,
                body={"chat_id": chat_id, "message_id": message_id, "text": text},
            )
        except TelegramBotError:
            await self._call(
                method=TELEGRAM_EDIT_CAPTION,
                body={"chat_id": chat_id, "message_id": message_id, "caption": text},
            )

    async def deliver(
        self,
        *,
        chat_id: int,
        text: str,
        files: list[InboundFile],
        reply_to_message_id: int | None = None,
    ) -> list[int]:
        chosen = files[:MAX_PHOTOS_PER_MESSAGE]
        skipped = len(files) - len(chosen)
        body = text
        if skipped:
            body = f"{body}\n[ещё вложений: {skipped}]".strip()
        sent: list[int] = []
        reply_to = reply_to_message_id
        if body:
            result = await self._call_piece(
                method=TELEGRAM_SEND_MESSAGE,
                body={"chat_id": chat_id, "text": body},
                reply_to_message_id=reply_to,
            )
            reply_to = None
            message_id = _message_id(result)
            if message_id is not None:
                sent.append(message_id)
        for item in chosen:
            message_id = await self._send_file(chat_id=chat_id, item=item, reply_to_message_id=reply_to)
            reply_to = None
            if message_id is not None:
                sent.append(message_id)
        logger.debug("сообщение доставлено в чат %s", chat_id)
        return sent

    async def _send_file(
        self,
        *,
        chat_id: int,
        item: InboundFile,
        reply_to_message_id: int | None,
    ) -> int | None:
        method, field, limit = _telegram_method(item.kind)
        try:
            async with self._session.get(item.url) as response:
                response.raise_for_status()
                content = await response.read()
        except aiohttp.ClientError as exc:
            logger.error("не скачалось вложение: %s", exc)
            result = await self._call_piece(
                method=TELEGRAM_SEND_MESSAGE,
                body={"chat_id": chat_id, "text": item.url},
                reply_to_message_id=reply_to_message_id,
            )
            return _message_id(result)
        if len(content) > limit:
            result = await self._call_piece(
                method=TELEGRAM_SEND_MESSAGE,
                body={"chat_id": chat_id, "text": FILE_TOO_LARGE_NOTE},
                reply_to_message_id=reply_to_message_id,
            )
            return _message_id(result)
        filename = item.filename or "file.bin"
        gif = animated_sticker_gif(content)
        if gif is not None and len(gif) <= TELEGRAM_FILE_LIMIT_BYTES:
            content = gif
            filename = "sticker.gif"
            method, field, _limit = _telegram_method(MEDIA_ANIMATION)
        elif filename == STICKER_FILE_NAME:
            flattened = sticker_on_white(content)
            if flattened is not None:
                content = flattened
                filename = "sticker.jpg"
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field(field, content, filename=filename)
        if reply_to_message_id is not None:
            form.add_field("reply_to_message_id", str(reply_to_message_id))
        try:
            result = await self._call(method=method, body=form)
        except TelegramBotError:
            if reply_to_message_id is None:
                raise
            logger.error("Telegram не принял reply, отправляю вложение без связи")
            plain = aiohttp.FormData()
            plain.add_field("chat_id", str(chat_id))
            plain.add_field(field, content, filename=filename)
            result = await self._call(method=method, body=plain)
        return _message_id(result)

    async def _call_piece(
        self,
        *,
        method: str,
        body: dict[str, Any],
        reply_to_message_id: int | None,
    ) -> dict[str, Any]:
        try:
            return await self._call(method=method, body=_with_reply(body, reply_to_message_id))
        except TelegramBotError:
            if reply_to_message_id is None:
                raise
            logger.error("Telegram не принял reply, отправляю без связи")
            return await self._call(method=method, body=body)

    async def _call(self, *, method: str, body: Any) -> dict[str, Any]:
        url = f"{TELEGRAM_API_URL}/bot{self._token}/{method}"
        if isinstance(body, aiohttp.FormData):
            response_ctx = self._session.post(url, data=body)
        elif body is None:
            response_ctx = self._session.get(url)
        else:
            response_ctx = self._session.post(url, json=body)
        async with response_ctx as response:
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict) or not payload.get(JsonKey.OK):
            description = ""
            if isinstance(payload, dict):
                description = str(payload.get(JsonKey.DESCRIPTION) or "")
            raise TelegramBotError(description or method)
        result = payload.get(JsonKey.RESULT)
        if isinstance(result, dict):
            return result
        return {}


def _with_reply(body: dict[str, Any], reply_to_message_id: int | None) -> dict[str, Any]:
    if reply_to_message_id is None:
        return body
    return {**body, "reply_to_message_id": reply_to_message_id}


def _message_id(result: dict[str, Any]) -> int | None:
    value = result.get("message_id")
    if isinstance(value, int):
        return value
    return None


def _telegram_method(kind: str) -> tuple[str, str, int]:
    methods = {
        MEDIA_PHOTO: (TELEGRAM_SEND_PHOTO, MEDIA_PHOTO, TELEGRAM_PHOTO_LIMIT_BYTES),
        MEDIA_VOICE: (TELEGRAM_SEND_VOICE, MEDIA_VOICE, TELEGRAM_FILE_LIMIT_BYTES),
        MEDIA_VIDEO: (TELEGRAM_SEND_VIDEO, MEDIA_VIDEO, TELEGRAM_FILE_LIMIT_BYTES),
        MEDIA_VIDEO_NOTE: (TELEGRAM_SEND_VIDEO_NOTE, MEDIA_VIDEO_NOTE, TELEGRAM_FILE_LIMIT_BYTES),
        MEDIA_AUDIO: (TELEGRAM_SEND_AUDIO, MEDIA_AUDIO, TELEGRAM_FILE_LIMIT_BYTES),
        MEDIA_ANIMATION: (TELEGRAM_SEND_ANIMATION, MEDIA_ANIMATION, TELEGRAM_FILE_LIMIT_BYTES),
        MEDIA_DOCUMENT: (TELEGRAM_SEND_DOCUMENT, MEDIA_DOCUMENT, TELEGRAM_FILE_LIMIT_BYTES),
    }
    return methods.get(kind, methods[MEDIA_DOCUMENT])
