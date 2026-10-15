import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from telethon.errors import FloodWaitError

from vk_tg_bridge.constants import (
    EMPTY_MESSAGE_TEXT,
    ERROR_REPEAT_SECONDS,
    HEARTBEAT_FILE,
    MAX_PHOTOS_PER_MESSAGE,
    POLL_FRESH_SECONDS,
    READ_REACTION,
    READ_REACTION_PAUSE_SECONDS,
    SERVICE_CHAT_KEY,
    SERVICE_MESSAGE_KEY,
    TYPING_COALESCE_SECONDS,
)
from vk_tg_bridge.health import (
    IncidentGate,
    brief,
    exception_detail,
    flood_wait_detail,
    format_incident,
    format_status,
    is_status_command,
    write_heartbeat,
)
from vk_tg_bridge.models import IncomingVkMessage, LoadedMessage, MessageLink, OutboundFile, PeerLink
from vk_tg_bridge.store import Store
from vk_tg_bridge.text import (
    append_version,
    chat_title,
    clip_message,
    mark_deleted,
    presence_about,
    title_needs_refresh,
    with_sender,
)
from vk_tg_bridge.vk_payload import add_command_targets, is_chat_peer, page_target
from vk_tg_bridge.telegram_bot import TelegramBot, TelegramBotError
from vk_tg_bridge.telegram_user import TelegramUser
from vk_tg_bridge.vk_client import VkApiError, VkClient

logger = logging.getLogger(__name__)


class Bridge:
    def __init__(
        self,
        *,
        store: Store,
        vk: VkClient,
        groups: TelegramUser,
        bot: TelegramBot,
    ) -> None:
        self._store = store
        self._vk = vk
        self._groups = groups
        self._bot = bot
        self._typing_at: dict[int, float] = {}
        self._abouts: dict[int, str] = {}
        self._poll_at: float | None = None
        self._incidents = IncidentGate(repeat_after=ERROR_REPEAT_SECONDS)
        self._reads: dict[int, int] = {}
        self._reads_ready = asyncio.Event()

    async def handle_presence(
        self,
        user_id: int,
        *,
        online: bool,
        at: datetime | None = None,
    ) -> None:
        if user_id <= 0 or is_chat_peer(user_id):
            return
        link = self._store.peer_by_vk(user_id)
        if link is None:
            return
        about = presence_about(online=online, moment=at)
        if about is None or self._abouts.get(link.telegram_chat_id) == about:
            return
        try:
            await self._groups.set_about(link.telegram_chat_id, about)
        except FloodWaitError:
            return
        except Exception:
            logger.exception("не удалось записать статус в чат %s", link.telegram_chat_id)
            return
        self._abouts[link.telegram_chat_id] = about
        logger.info("описание %s: %s", link.telegram_chat_id, about)

    async def refresh_presence(self) -> None:
        user_ids = [
            link.vk_peer_id
            for link in self._store.peers()
            if link.vk_peer_id > 0 and not is_chat_peer(link.vk_peer_id)
        ]
        if not user_ids:
            return
        try:
            found = await self._vk.presence_for_many(user_ids)
        except VkApiError as exc:
            logger.error("не прочитались статусы: %s", exc)
            return
        for user_id, (online, stamp) in found.items():
            moment = datetime.fromtimestamp(stamp) if stamp else None
            await self.handle_presence(user_id, online=online, at=moment)

    async def report(
        self,
        *,
        key: str,
        title: str,
        detail: str,
        now: datetime | None = None,
    ) -> None:
        moment = now or datetime.now()
        stamp = moment.timestamp()
        if not self._incidents.due(key, stamp):
            return
        logger.error("ошибка «%s»: %s", title, detail)
        chat_id = self._store.read_runtime_int(SERVICE_CHAT_KEY)
        if chat_id is None:
            logger.error("некуда отправить ошибку «%s»: служебный чат ещё не создан", title)
            return
        try:
            await self._bot.send_text(
                chat_id=chat_id,
                text=format_incident(now=moment, title=title, detail=detail),
            )
        except TelegramBotError as exc:
            logger.error("не удалось отправить ошибку «%s» в служебный чат: %s", title, exc)
        self._incidents.mark(key, stamp)

    def note_poll(self) -> None:
        self._poll_at = time.monotonic()

    def poll_state(self) -> str:
        if self._poll_at is None:
            return "starting"
        if time.monotonic() - self._poll_at <= POLL_FRESH_SECONDS:
            return "ok"
        return "stale"

    async def pulse(self, *, now: datetime | None = None) -> str:
        moment = now or datetime.now()
        chat_id = await self._service_chat()
        text = format_status(
            now=moment,
            poll_state=self.poll_state(),
            token_until=self._token_until(),
        )
        message_id = self._store.read_runtime_int(SERVICE_MESSAGE_KEY)
        if message_id is None:
            message_id = await self._publish_status(chat_id, text)
        else:
            try:
                await self._bot.revise(chat_id=chat_id, message_id=message_id, text=text)
            except TelegramBotError:
                logger.error("статус в чате %s не обновился, отправляю заново", chat_id)
                message_id = await self._publish_status(chat_id, text)
        write_heartbeat(
            self._heartbeat_path(),
            updated_at=moment.timestamp(),
            chat_id=chat_id,
        )
        return text

    async def _service_chat(self) -> int:
        saved = self._store.read_runtime_int(SERVICE_CHAT_KEY)
        if saved is not None and await self._groups.has_chat(saved):
            return saved
        if saved is not None:
            logger.info("служебный чат %s недоступен этой сессии, создаю новый", saved)
            self._store.delete_runtime(SERVICE_MESSAGE_KEY)
        chat_id = await self._groups.open_service_chat()
        self._store.save_runtime(SERVICE_CHAT_KEY, str(chat_id))
        return chat_id

    async def _publish_status(self, chat_id: int, text: str) -> int:
        message_id = await self._bot.send_text(chat_id=chat_id, text=text)
        self._store.save_runtime(SERVICE_MESSAGE_KEY, str(message_id))
        await self._groups.pin(chat_id, message_id)
        return message_id

    def _token_until(self) -> datetime | None:
        stamp = self._vk.token_expires_at()
        if not stamp:
            return None
        return datetime.fromtimestamp(stamp)

    def _heartbeat_path(self) -> Path:
        return Path(self._store.path).parent / HEARTBEAT_FILE

    def _is_status_request(self, chat_id: int, text: str) -> bool:
        if not is_status_command(text):
            return False
        return chat_id == self._store.read_runtime_int(SERVICE_CHAT_KEY)

    async def open_pages(self, *, chat_id: int, targets: list[str]) -> None:
        if not targets:
            await self._bot.send_text(chat_id=chat_id, text="Формат: /a https://vk.com/id1")
            return
        lines: list[str] = []
        for target in targets:
            parsed = page_target(target)
            if parsed is None:
                lines.append(f"не ссылка VK: {target}")
                continue
            try:
                peer_id = parsed if isinstance(parsed, int) else await self._vk.peer_by_screen_name(parsed)
            except VkApiError as exc:
                lines.append(f"не открылось {target}: {exc}")
                await self.report(
                    key=f"resolve:{target}",
                    title="ссылка VK",
                    detail=f"{target}\n{exception_detail(exc)}",
                )
                continue
            if peer_id is None:
                lines.append(f"не нашёл: {target}")
                continue
            existed = self._store.peer_by_vk(peer_id) is not None
            try:
                link = await self._ensure_chat(peer_id)
            except FloodWaitError as exc:
                notice = flood_wait_detail(exc.seconds, action="создать группу")
                lines.append(notice)
                await self.report(
                    key=f"create-group:{peer_id}",
                    title="создание чата",
                    detail=f"{notice}\nссылка: {target}",
                )
                break
            except Exception as exc:
                lines.append(f"не открылось {target}: {exc}")
                await self.report(
                    key=f"create-group:{peer_id}",
                    title="создание чата",
                    detail=f"ссылка: {target}\n{exception_detail(exc)}",
                )
                continue
            verb = "уже есть" if existed else "создан"
            lines.append(f"{verb}: {link.title}")
        await self._bot.send_text(chat_id=chat_id, text="\n".join(lines))

    async def handle_typing(self, peer_id: int, *, now: float | None = None) -> None:
        link = self._store.peer_by_vk(peer_id)
        if link is None:
            return
        moment = time.monotonic() if now is None else now
        previous = self._typing_at.get(peer_id)
        if previous is not None and moment - previous < TYPING_COALESCE_SECONDS:
            return
        try:
            await self._bot.show_typing(link.telegram_chat_id)
        except TelegramBotError as exc:
            logger.error("не удалось показать набор в чате %s: %s", link.telegram_chat_id, exc)
            return
        self._typing_at[peer_id] = moment

    async def handle_vk(self, message: IncomingVkMessage) -> None:
        link = await self._ensure_chat(message.peer_id)
        from_id = None
        loaded = LoadedMessage(text=message.text)
        try:
            loaded = await self._vk.load_message(message.message_id)
            text = loaded.text or message.text
            files = list(loaded.files)
            from_id = loaded.from_id
        except VkApiError as exc:
            logger.error(
                "не прочиталось сообщение %s: %s",
                message.message_id,
                exc,
            )
            text = message.text
            files = []
            loaded = LoadedMessage(text=text)
        if not text.strip() and not files:
            text = EMPTY_MESSAGE_TEXT
        elif text.strip():
            text = clip_message(text)
        else:
            text = ""
        if is_chat_peer(message.peer_id):
            text = with_sender(text, await self._vk.sender_name(from_id))
        reply_to = self._telegram_target(message.peer_id, loaded.reply_conversation_message_id)
        delivered = await self._bot.deliver(
            chat_id=link.telegram_chat_id,
            text=text,
            files=files,
            reply_to_message_id=reply_to,
        )
        self._remember_vk(
            chat_id=link.telegram_chat_id,
            telegram_ids=delivered,
            peer_id=message.peer_id,
            vk_message_id=message.message_id,
            conversation_message_id=loaded.conversation_message_id,
            body=text,
        )

    async def handle_reply(
        self,
        *,
        chat_id: int,
        text: str,
        files: list[OutboundFile] | None = None,
        telegram_message_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        if self._is_status_request(chat_id, text):
            try:
                status = await self.pulse()
                await self._bot.send_text(chat_id=chat_id, text=status)
            except Exception:
                logger.exception("не удалось ответить на статус")
            return
        targets = add_command_targets(text)
        if targets is not None:
            try:
                await self.open_pages(chat_id=chat_id, targets=targets)
            except Exception:
                logger.exception("не удалось открыть чаты по ссылкам")
            return
        outbound = list(files or [])
        cleaned = text.strip()
        if not cleaned and not outbound:
            return
        link = self._store.peer_by_telegram(chat_id)
        if link is None:
            logger.debug("исходящее из неизвестного чата %s", chat_id)
            return
        extra = outbound[MAX_PHOTOS_PER_MESSAGE:]
        outbound = outbound[:MAX_PHOTOS_PER_MESSAGE]
        if extra:
            cleaned = f"{cleaned}\n[ещё вложений: {len(extra)}]".strip()
        reply_to = self._vk_target(chat_id, reply_to_message_id, peer_id=link.vk_peer_id)
        body = clip_message(cleaned) if cleaned else ""
        try:
            sent = await self._vk.send_reply(
                peer_id=link.vk_peer_id,
                text=body,
                files=outbound,
                reply_to=reply_to,
            )
        except VkApiError as exc:
            if reply_to is None:
                await self._report_vk_send(link.vk_peer_id, body, exc)
                return
            logger.error("VK не принял reply для peer %s, отправляю без связи", link.vk_peer_id)
            try:
                sent = await self._vk.send_reply(peer_id=link.vk_peer_id, text=body, files=outbound)
            except VkApiError as retry_exc:
                await self._report_vk_send(link.vk_peer_id, body, retry_exc)
                return
        if sent is None:
            return
        if telegram_message_id is not None and sent.conversation_message_id is not None:
            self._store.save_message_link(
                telegram_chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                vk_peer_id=link.vk_peer_id,
                vk_message_id=sent.message_id,
                vk_conversation_message_id=sent.conversation_message_id,
                outgoing=True,
                body=body,
            )
        if telegram_message_id is not None:
            await self._mark_delivered(chat_id, telegram_message_id)

    async def _mark_delivered(self, chat_id: int, message_id: int) -> None:
        try:
            await self._groups.allow_reactions(chat_id)
            await self._groups.react(chat_id=chat_id, message_id=message_id)
        except Exception:
            logger.exception("не удалось поставить отметку доставки на %s в чате %s", message_id, chat_id)

    def note_read(self, peer_id: int, up_to: int) -> None:
        current = self._reads.get(peer_id)
        if current is None or up_to > current:
            self._reads[peer_id] = up_to
        self._reads_ready.set()

    async def wait_reads(self) -> None:
        await self._reads_ready.wait()

    async def apply_pending_reads(self) -> None:
        self._reads_ready.clear()
        while self._reads:
            peer_id, up_to = self._reads.popitem()
            await self.mark_read(peer_id, up_to)

    def wake_reads(self) -> None:
        if self._reads:
            self._reads_ready.set()

    async def mark_read(self, peer_id: int, up_to: int) -> None:
        pending = self._store.outgoing_unread(peer_id=peer_id, up_to=up_to)
        for index, link in enumerate(pending):
            if index:
                await asyncio.sleep(READ_REACTION_PAUSE_SECONDS)
            try:
                await self._groups.allow_reactions(link.telegram_chat_id)
                await self._groups.react(
                    chat_id=link.telegram_chat_id,
                    message_id=link.telegram_message_id,
                    emoji=READ_REACTION,
                )
            except FloodWaitError as exc:
                logger.error("Telegram просит подождать %s с перед реакцией", exc.seconds)
                return
            except Exception:
                logger.exception(
                    "не удалось отметить прочтение %s в чате %s",
                    link.telegram_message_id,
                    link.telegram_chat_id,
                )
                continue
            self._store.mark_link_read(
                chat_id=link.telegram_chat_id,
                message_id=link.telegram_message_id,
            )

    async def refresh_saved(self, *, groups_only: bool = False) -> int:
        updated = 0
        for link in self._store.peers():
            group = is_chat_peer(link.vk_peer_id)
            if groups_only and not group:
                continue
            title = chat_title(
                await self._vk.title_for(link.vk_peer_id),
                link.vk_peer_id,
                group=group,
            )
            photo = await self._vk.avatar_for(link.vk_peer_id)
            try:
                await self._groups.refresh_group(link.telegram_chat_id, title=title, photo=photo)
            except FloodWaitError as exc:
                logger.error("Telegram просит подождать %s с перед переименованием", exc.seconds)
                continue
            self._store.rename_peer(link.vk_peer_id, title)
            updated += 1
            logger.info("чат %s обновлён", link.telegram_chat_id)
        return updated

    async def _report_vk_send(self, peer_id: int, body: str, exc: VkApiError) -> None:
        preview = brief(body) if body else "без текста"
        await self.report(
            key=f"vk-send:{peer_id}",
            title="отправка в VK",
            detail=f"peer {peer_id}\nтекст: {preview}\n{exception_detail(exc)}",
        )

    def _telegram_target(self, peer_id: int, conversation_message_id: int | None) -> int | None:
        if conversation_message_id is None:
            return None
        origin = self._store.link_by_vk(peer_id=peer_id, conversation_message_id=conversation_message_id)
        if origin is None:
            return None
        return origin.telegram_message_id

    def _vk_target(self, chat_id: int, message_id: int | None, *, peer_id: int) -> int | None:
        if message_id is None:
            return None
        origin = self._store.link_by_telegram(chat_id=chat_id, message_id=message_id)
        if origin is None or origin.vk_peer_id != peer_id:
            return None
        return origin.vk_conversation_message_id

    async def handle_edit(self, *, chat_id: int, telegram_message_id: int, text: str) -> None:
        link = self._store.link_by_telegram(chat_id=chat_id, message_id=telegram_message_id)
        if link is None or not link.outgoing:
            return
        try:
            await self._vk.edit_message(
                peer_id=link.vk_peer_id,
                message_id=link.vk_message_id,
                conversation_message_id=link.vk_conversation_message_id,
                text=text.strip(),
            )
        except VkApiError as exc:
            logger.error("не удалось поправить сообщение %s в VK: %s", link.vk_message_id, exc)
            await self.report(
                key=f"vk-edit:{link.vk_message_id}",
                title="правка в VK",
                detail=f"peer {link.vk_peer_id}, сообщение {link.vk_message_id}\n{exception_detail(exc)}",
            )

    async def handle_delete(self, *, chat_id: int, telegram_message_id: int) -> None:
        link = self._store.link_by_telegram(chat_id=chat_id, message_id=telegram_message_id)
        if link is None or not link.outgoing:
            return
        try:
            await self._vk.delete_message(
                peer_id=link.vk_peer_id,
                conversation_message_id=link.vk_conversation_message_id,
            )
        except VkApiError as exc:
            logger.error("не удалось удалить сообщение %s в VK: %s", link.vk_message_id, exc)
            await self.report(
                key=f"vk-delete:{link.vk_message_id}",
                title="удаление в VK",
                detail=f"peer {link.vk_peer_id}, сообщение {link.vk_message_id}\n{exception_detail(exc)}",
            )

    async def handle_vk_edit(
        self,
        *,
        peer_id: int,
        message_id: int,
        at: datetime | None = None,
    ) -> None:
        primary = self._incoming_copy(peer_id=peer_id, message_id=message_id)
        if primary is None:
            return
        try:
            loaded = await self._vk.load_message(message_id)
        except VkApiError as exc:
            logger.error("не прочиталась правка %s: %s", message_id, exc)
            return
        await self._keep_version(primary, append_version(primary.body, loaded.text, at or datetime.now()))

    async def handle_vk_delete(
        self,
        *,
        peer_id: int,
        message_id: int,
        at: datetime | None = None,
    ) -> None:
        primary = self._incoming_copy(peer_id=peer_id, message_id=message_id)
        if primary is None:
            return
        await self._keep_version(primary, mark_deleted(primary.body, at or datetime.now()))

    def _incoming_copy(self, *, peer_id: int, message_id: int) -> MessageLink | None:
        incoming = [
            link
            for link in self._store.links_by_vk_message(peer_id=peer_id, message_id=message_id)
            if not link.outgoing
        ]
        if not incoming:
            return None
        return incoming[0]

    async def _keep_version(self, link: MessageLink, text: str) -> None:
        if text == link.body:
            return
        try:
            await self._bot.revise(
                chat_id=link.telegram_chat_id,
                message_id=link.telegram_message_id,
                text=text,
            )
        except Exception as exc:
            logger.exception(
                "не удалось обновить копию %s в чате %s",
                link.telegram_message_id,
                link.telegram_chat_id,
            )
            await self.report(
                key=f"revise:{link.telegram_chat_id}:{link.telegram_message_id}",
                title="правка из VK",
                detail=(
                    f"чат {link.telegram_chat_id}, сообщение {link.telegram_message_id}\n"
                    f"{exception_detail(exc)}"
                ),
            )
            return
        self._store.update_body(
            chat_id=link.telegram_chat_id,
            message_id=link.telegram_message_id,
            body=text,
        )

    def _remember_vk(
        self,
        *,
        chat_id: int,
        telegram_ids: list[int],
        peer_id: int,
        vk_message_id: int,
        conversation_message_id: int | None,
        body: str = "",
    ) -> None:
        if conversation_message_id is None:
            return
        for index, telegram_message_id in enumerate(telegram_ids):
            self._store.save_message_link(
                telegram_chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                vk_peer_id=peer_id,
                vk_message_id=vk_message_id,
                vk_conversation_message_id=conversation_message_id,
                body=body if index == 0 else "",
            )

    async def _ensure_chat(self, peer_id: int) -> PeerLink:
        group = is_chat_peer(peer_id)
        link = self._store.peer_by_vk(peer_id)
        if link is None or title_needs_refresh(link.title, group=group):
            title = chat_title(await self._vk.title_for(peer_id), peer_id, group=group)
            photo = await self._vk.avatar_for(peer_id)
            if link is None:
                chat_id = await self._groups.create_group(title, photo=photo)
                link = self._store.save_peer(
                    vk_peer_id=peer_id,
                    title=title,
                    telegram_chat_id=chat_id,
                    bot_invited=False,
                )
            else:
                try:
                    await self._groups.refresh_group(link.telegram_chat_id, title=title, photo=photo)
                except FloodWaitError as exc:
                    logger.error("Telegram просит подождать %s с перед переименованием", exc.seconds)
                else:
                    self._store.rename_peer(peer_id, title)
                    saved = self._store.peer_by_vk(peer_id)
                    if saved is not None:
                        link = saved
        if not link.bot_invited:
            await self._groups.invite_bot(link.telegram_chat_id)
            self._store.mark_bot_invited(peer_id)
            saved = self._store.peer_by_vk(peer_id)
            if saved is not None:
                link = saved
        return link
