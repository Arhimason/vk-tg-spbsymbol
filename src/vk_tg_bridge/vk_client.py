import logging
import secrets
import time
from typing import Any

import aiohttp

from vk_tg_bridge.constants import (
    LONG_POLL_MODE,
    PRESENCE_BATCH,
    LONG_POLL_TIMEOUT_BUFFER_SECONDS,
    LONG_POLL_VERSION,
    LONG_POLL_WAIT_SECONDS,
    VK_API_URL,
    MEDIA_PHOTO,
    MEDIA_VIDEO,
    MEDIA_VIDEO_NOTE,
    MEDIA_VOICE,
    VK_VIDEO_MESSAGE_SHAPE_ID,
    VK_API_VERSION,
    VK_LANG_RU,
    VK_AUDIO_MESSAGE,
    VK_DOC,
    VK_ERROR_AUTH_FAILED,
    VK_VIDEO_FILE_FIELD,
    VK_EXCHANGE_URL,
    VK_TOKEN_URL,
    JsonKey,
    VkMethod,
)
from vk_tg_bridge.models import (
    LoadedMessage,
    LongPollServer,
    LongPollTick,
    OutboundFile,
    SentVkMessage,
    VkCredentials,
    tick_from_payload,
)
from vk_tg_bridge.store import Store
from vk_tg_bridge.vk_auth import (
    VkAuthError,
    build_exchange_form,
    build_refresh_form,
    credentials_from_exchange,
    credentials_from_refresh,
    payload_from_exchange_response,
    should_refresh,
    write_session,
)
from vk_tg_bridge.avatar import badge_avatar
from vk_tg_bridge.vk_payload import (
    attachment_tokens,
    avatar_url_from_chat,
    avatar_url_from_group,
    avatar_url_from_user,
    is_chat_peer,
    conversation_message_id,
    peer_from_resolved,
    presence_from_user,
    message_body,
    reply_conversation_id,
    saved_photo_attachment,
    saved_video_attachment,
    saved_video_message_attachment,
    saved_voice_attachment,
    title_from_chat,
    title_from_groups,
    title_from_users,
    vk_reply_forward,
)

logger = logging.getLogger(__name__)


class VkApiError(Exception):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(message)


class VkClient:
    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        store: Store,
        credentials: VkCredentials,
        session_path: str | None = None,
    ) -> None:
        self._session = session
        self._store = store
        self._credentials = credentials
        self._session_path = session_path
        self._sender_names: dict[int, str] = {}

    def token_expires_at(self) -> float | None:
        return self._credentials.expires_at

    async def open_long_poll(self) -> LongPollServer:
        payload = await self.call(
            method=VkMethod.GET_LONG_POLL_SERVER,
            params={"need_pts": 0, "lp_version": LONG_POLL_VERSION},
        )
        server = str(payload[JsonKey.SERVER])
        if not server.startswith("https://"):
            server = f"https://{server}"
        ts = self._store.read_cursor()
        if ts is None:
            ts = int(payload[JsonKey.TS])
        return LongPollServer(server=server, key=str(payload[JsonKey.KEY]), ts=ts)

    async def check(self, server: LongPollServer) -> LongPollTick:
        timeout = aiohttp.ClientTimeout(
            total=LONG_POLL_WAIT_SECONDS + LONG_POLL_TIMEOUT_BUFFER_SECONDS,
        )
        params = {
            "act": "a_check",
            "key": server.key,
            "ts": server.ts,
            "wait": LONG_POLL_WAIT_SECONDS,
            "mode": LONG_POLL_MODE,
            "version": LONG_POLL_VERSION,
        }
        async with self._session.get(
            server.server,
            params=params,
            timeout=timeout,
        ) as response:
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise VkApiError(code=0, message="long poll вернул не объект")
        return tick_from_payload(payload)

    async def load_message(self, message_id: int) -> LoadedMessage:
        payload = await self.call(
            method=VkMethod.GET_BY_ID,
            params={"message_ids": message_id},
        )
        items = payload.get(JsonKey.ITEMS) if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            return LoadedMessage(text="", files=[])
        text, files = message_body(items[0])
        from_id = items[0].get("from_id")
        return LoadedMessage(
            text=text,
            files=files,
            from_id=from_id if isinstance(from_id, int) else None,
            conversation_message_id=conversation_message_id(items[0]),
            reply_conversation_message_id=reply_conversation_id(items[0]),
        )

    async def peer_by_screen_name(self, screen_name: str) -> int | None:
        payload = await self.call(
            method=VkMethod.RESOLVE_SCREEN_NAME,
            params={"screen_name": screen_name},
        )
        return peer_from_resolved(payload)

    async def presence_for(self, user_id: int) -> tuple[bool, int | None] | None:
        found = await self.presence_for_many([user_id])
        return found.get(user_id)

    async def presence_for_many(self, user_ids: list[int]) -> dict[int, tuple[bool, int | None]]:
        found: dict[int, tuple[bool, int | None]] = {}
        for offset in range(0, len(user_ids), PRESENCE_BATCH):
            chunk = user_ids[offset:offset + PRESENCE_BATCH]
            payload = await self.call(
                method=VkMethod.USERS_GET,
                params={
                    "user_ids": ",".join(str(user_id) for user_id in chunk),
                    "fields": "online,last_seen",
                },
            )
            found.update(_presence_map(payload))
        return found

    async def sender_name(self, user_id: int | None) -> str:
        if user_id is None:
            return ""
        cached = self._sender_names.get(user_id)
        if cached is not None:
            return cached
        try:
            if user_id < 0:
                payload = await self.call(
                    method=VkMethod.GROUPS_GET_BY_ID,
                    params={"group_id": abs(user_id)},
                )
                name = title_from_groups(payload)
            else:
                payload = await self.call(
                    method=VkMethod.USERS_GET,
                    params={"user_ids": user_id},
                )
                name = title_from_users(payload)
        except VkApiError as exc:
            logger.error("не удалось получить отправителя %s: %s", user_id, exc)
            name = ""
        if not name:
            name = f"VK {user_id}"
        self._sender_names[user_id] = name
        return name

    async def edit_message(
        self,
        *,
        peer_id: int,
        message_id: int,
        conversation_message_id: int,
        text: str,
    ) -> None:
        attachments = await self._existing_attachments(message_id)
        params: dict[str, Any] = {
            "peer_id": peer_id,
            "cmid": conversation_message_id,
            "message": text,
            "keep_forward_messages": 1,
            "keep_snippets": 1,
        }
        if attachments:
            params["attachment"] = attachments
        await self.call(method=VkMethod.EDIT, params=params)

    async def delete_message(self, *, peer_id: int, conversation_message_id: int) -> None:
        await self.call(
            method=VkMethod.DELETE,
            params={
                "peer_id": peer_id,
                "cmids": conversation_message_id,
                "delete_for_all": 1,
            },
        )

    async def _existing_attachments(self, message_id: int) -> str:
        payload = await self.call(
            method=VkMethod.GET_BY_ID,
            params={"message_ids": message_id},
        )
        items = payload.get(JsonKey.ITEMS) if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            return ""
        return attachment_tokens(items[0])

    async def send_text(self, *, peer_id: int, text: str) -> None:
        await self.send_reply(peer_id=peer_id, text=text, files=[])

    async def send_reply(
        self,
        *,
        peer_id: int,
        text: str,
        files: list[OutboundFile],
        reply_to: int | None = None,
    ) -> SentVkMessage | None:
        attachments: list[str] = []
        for item in files:
            attachments.append(await self._upload_outbound(peer_id=peer_id, item=item))
        params: dict[str, Any] = {
            "peer_id": peer_id,
            "random_id": secrets.randbits(31),
        }
        if text:
            params["message"] = text
        if attachments:
            params["attachment"] = ",".join(attachments)
        if reply_to is not None:
            params["forward"] = vk_reply_forward(peer_id=peer_id, conversation_message_id=reply_to)
        if "message" not in params and "attachment" not in params:
            return None
        sent = await self.call(method=VkMethod.SEND, params=params)
        if not isinstance(sent, int):
            logger.error("VK не вернул id сообщения для peer %s", peer_id)
            return None
        logger.info("ответ отправлен в VK peer %s", peer_id)
        try:
            loaded = await self.load_message(sent)
        except VkApiError as exc:
            logger.error("не прочитался id ответа %s: %s", sent, exc)
            return SentVkMessage(message_id=sent)
        return SentVkMessage(message_id=sent, conversation_message_id=loaded.conversation_message_id)

    async def _upload_photo(self, *, peer_id: int, content: bytes) -> str:
        server = await self.call(
            method=VkMethod.GET_MESSAGES_UPLOAD_SERVER,
            params={"peer_id": peer_id},
        )
        uploaded = await self._post_upload(
            url=_upload_url(server),
            field=JsonKey.PHOTO,
            content=content,
            filename="photo.jpg",
        )
        saved = await self.call(
            method=VkMethod.SAVE_MESSAGES_PHOTO,
            params={
                "server": uploaded.get(JsonKey.SERVER),
                "photo": uploaded.get(JsonKey.PHOTO),
                "hash": uploaded.get(JsonKey.HASH),
            },
        )
        return saved_photo_attachment(saved)

    async def _upload_voice(self, *, peer_id: int, content: bytes) -> str:
        server = await self.call(
            method=VkMethod.GET_DOCS_UPLOAD_SERVER,
            params={"peer_id": peer_id, "type": VK_AUDIO_MESSAGE},
        )
        uploaded = await self._post_upload(
            url=_upload_url(server),
            field=JsonKey.FILE,
            content=content,
            filename="voice.ogg",
        )
        saved = await self.call(
            method=VkMethod.DOCS_SAVE,
            params={"file": uploaded.get(JsonKey.FILE)},
        )
        return saved_voice_attachment(saved)

    async def _upload_outbound(self, *, peer_id: int, item: OutboundFile) -> str:
        if item.kind == MEDIA_PHOTO:
            return await self._upload_photo(peer_id=peer_id, content=item.content)
        if item.kind == MEDIA_VOICE:
            return await self._upload_voice(peer_id=peer_id, content=item.content)
        if item.kind == MEDIA_VIDEO_NOTE:
            return await self._upload_video_note(content=item.content, filename=item.filename)
        if item.kind == MEDIA_VIDEO:
            return await self._upload_video(content=item.content, filename=item.filename)
        return await self._upload_document(
            peer_id=peer_id,
            content=item.content,
            filename=item.filename,
        )

    async def _upload_video_note(self, *, content: bytes, filename: str) -> str:
        try:
            saved = await self.call(
                method=VkMethod.GET_VIDEO_MESSAGE_UPLOAD_INFO,
                params={"shape_id": VK_VIDEO_MESSAGE_SHAPE_ID},
            )
        except VkApiError:
            logger.warning("кружок VK недоступен, отправляю обычным видео")
            return await self._upload_video(content=content, filename=filename)
        await self._post_upload(
            url=_upload_url(saved),
            field=VK_VIDEO_FILE_FIELD,
            content=content,
            filename=filename,
        )
        return saved_video_message_attachment(saved)

    async def _upload_video(self, *, content: bytes, filename: str) -> str:
        saved = await self.call(
            method=VkMethod.VIDEO_SAVE,
            params={"name": filename, "is_private": 1, "wallpost": 0},
        )
        await self._post_upload(
            url=_upload_url(saved),
            field=VK_VIDEO_FILE_FIELD,
            content=content,
            filename=filename,
        )
        return saved_video_attachment(saved)

    async def _upload_document(self, *, peer_id: int, content: bytes, filename: str) -> str:
        server = await self.call(
            method=VkMethod.GET_DOCS_UPLOAD_SERVER,
            params={"peer_id": peer_id, "type": VK_DOC},
        )
        uploaded = await self._post_upload(
            url=_upload_url(server),
            field=JsonKey.FILE,
            content=content,
            filename=filename,
        )
        saved = await self.call(
            method=VkMethod.DOCS_SAVE,
            params={"file": uploaded.get(JsonKey.FILE), "title": filename},
        )
        return saved_voice_attachment(saved)

    async def _post_upload(
        self,
        *,
        url: str,
        field: str,
        content: bytes,
        filename: str,
    ) -> dict[str, Any]:
        form = aiohttp.FormData()
        form.add_field(field, content, filename=filename)
        async with self._session.post(url, data=form) as response:
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise VkApiError(code=0, message="загрузка VK вернула не объект")
        return payload

    async def avatar_for(self, peer_id: int) -> bytes | None:
        try:
            url = await self._avatar_url(peer_id)
        except VkApiError as exc:
            logger.error("не удалось получить аватар peer %s: %s", peer_id, exc)
            return None
        if not url:
            return None
        content = await self._download_avatar(url)
        if content is None:
            return None
        return badge_avatar(content)

    async def _avatar_url(self, peer_id: int) -> str | None:
        if is_chat_peer(peer_id):
            payload = await self.call(
                method=VkMethod.CONVERSATIONS_BY_ID,
                params={"peer_ids": peer_id},
            )
            return avatar_url_from_chat(payload)
        if peer_id < 0:
            payload = await self.call(
                method=VkMethod.GROUPS_GET_BY_ID,
                params={"group_id": abs(peer_id), "fields": "photo_200,photo_400"},
            )
            return avatar_url_from_group(payload)
        payload = await self.call(
            method=VkMethod.USERS_GET,
            params={"user_ids": peer_id, "fields": "photo_200,photo_max_orig,has_photo"},
        )
        return avatar_url_from_user(payload)

    async def _download_avatar(self, url: str) -> bytes | None:
        try:
            async with self._session.get(url) as response:
                response.raise_for_status()
                content = await response.read()
        except aiohttp.ClientError as exc:
            logger.error("не скачался аватар VK: %s", exc)
            return None
        if not content:
            return None
        return content

    async def title_for(self, peer_id: int) -> str:
        try:
            if is_chat_peer(peer_id):
                payload = await self.call(
                    method=VkMethod.CONVERSATIONS_BY_ID,
                    params={"peer_ids": peer_id},
                )
                return title_from_chat(payload)
            if peer_id < 0:
                payload = await self.call(
                    method=VkMethod.GROUPS_GET_BY_ID,
                    params={"group_id": abs(peer_id)},
                )
                return title_from_groups(payload)
            payload = await self.call(
                method=VkMethod.USERS_GET,
                params={"user_ids": peer_id},
            )
            return title_from_users(payload)
        except VkApiError as exc:
            logger.error("не удалось получить имя peer %s: %s", peer_id, exc)
            return ""

    async def call(self, *, method: str, params: dict[str, Any]) -> Any:
        await self._ensure_fresh()
        try:
            return await self._request(method=method, params=params)
        except VkAuthError:
            if not self._credentials.can_renew():
                raise
            logger.info("VK вернул ошибку авторизации, обновляю токен")
            await self.refresh()
            return await self._request(method=method, params=params)

    async def refresh(self) -> None:
        if self._credentials.can_exchange():
            await self._refresh_exchange()
            return
        form = build_refresh_form(self._credentials)
        async with self._session.post(
            VK_TOKEN_URL,
            data=form.model_dump(),
        ) as response:
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise VkAuthError(code=0, message="VK ID вернул не объект")
        updated = credentials_from_refresh(
            payload,
            previous=self._credentials,
            expected_state=form.state,
            now=time.time(),
        )
        self._credentials = updated
        self._store.save_credentials(updated)
        logger.info("токен VK обновлён")

    async def _refresh_exchange(self) -> None:
        form = build_exchange_form(self._credentials)
        async with self._session.post(
            VK_EXCHANGE_URL,
            data=form.model_dump(),
            allow_redirects=False,
        ) as response:
            location = response.headers.get("Location", "")
            raw = await response.text()
            status = response.status
        payload = payload_from_exchange_response(status=status, location=location, body=raw)
        updated = credentials_from_exchange(
            payload,
            previous=self._credentials,
            now=time.time(),
        )
        self._credentials = updated
        self._store.save_credentials(updated)
        if self._session_path:
            write_session(self._session_path, updated)
        logger.info("токен VK продлён")

    async def _ensure_fresh(self) -> None:
        if should_refresh(self._credentials, now=time.time()):
            logger.debug("срок access token близок, обновляю заранее")
            await self.refresh()

    async def _request(self, *, method: str, params: dict[str, Any]) -> Any:
        headers = {"Authorization": f"Bearer {self._credentials.access_token}"}
        query = vk_api_query(params)
        async with self._session.get(
            f"{VK_API_URL}{method}",
            params=query,
            headers=headers,
        ) as response:
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise VkApiError(code=0, message=f"{method} вернул не объект")
        error = payload.get(JsonKey.ERROR)
        if isinstance(error, dict):
            code = int(error.get(JsonKey.ERROR_CODE) or 0)
            message = str(error.get(JsonKey.ERROR_MSG) or method)
            if code == VK_ERROR_AUTH_FAILED:
                raise VkAuthError(code=code, message=message)
            raise VkApiError(code=code, message=message)
        if JsonKey.RESPONSE not in payload:
            raise VkApiError(code=0, message=f"{method} без поля response")
        return payload[JsonKey.RESPONSE]


def vk_api_query(params: dict[str, Any]) -> dict[str, Any]:
    return {**params, "v": VK_API_VERSION, "lang": VK_LANG_RU}


def _presence_map(payload: Any) -> dict[int, tuple[bool, int | None]]:
    if not isinstance(payload, list):
        return {}
    found: dict[int, tuple[bool, int | None]] = {}
    for user in payload:
        if not isinstance(user, dict) or not isinstance(user.get(JsonKey.ID), int):
            continue
        parsed = presence_from_user([user])
        if parsed is not None:
            found[user[JsonKey.ID]] = parsed
    return found


def _upload_url(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise VkApiError(code=0, message="VK не вернул сервер загрузки")
    url = payload.get(JsonKey.UPLOAD_URL)
    if not isinstance(url, str) or not url:
        raise VkApiError(code=0, message="VK не вернул сервер загрузки")
    return url
