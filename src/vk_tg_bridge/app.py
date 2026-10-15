import asyncio
import logging
from datetime import datetime

import aiohttp
from telethon.errors import FloodWaitError

from vk_tg_bridge.bridge import Bridge
from vk_tg_bridge.health import brief, exception_detail, flood_wait_detail
from vk_tg_bridge.constants import (
    DELIVERY_ATTEMPTS,
    HEARTBEAT_INTERVAL_SECONDS,
    PRESENCE_POLL_SECONDS,
    RETRY_PAUSE_SECONDS,
    VK_SESSION_PATH,
)
from vk_tg_bridge.models import IncomingVkMessage, LongPollServer, Settings
from vk_tg_bridge.store import Store
from vk_tg_bridge.telegram_bot import TelegramBot
from vk_tg_bridge.telegram_user import TelegramUser
from vk_tg_bridge.vk_auth import VkAuthError, read_session, resolve_credentials
from vk_tg_bridge.vk_client import VkApiError, VkClient
from vk_tg_bridge.vk_payload import (
    LongPollStep,
    long_poll_step,
    parse_message_deleted,
    parse_message_edit,
    parse_new_message,
    parse_presence,
    parse_outgoing_read,
    parse_typing,
)

logger = logging.getLogger(__name__)


async def refresh_existing(settings: Settings, *, groups_only: bool = False) -> int:
    store = Store(settings.database_path)
    credentials = resolve_credentials(
        store.read_credentials(),
        settings,
        session=read_session(VK_SESSION_PATH),
    )
    async with aiohttp.ClientSession() as session:
        vk = VkClient(
            session=session,
            store=store,
            credentials=credentials,
            session_path=VK_SESSION_PATH,
        )
        bot = TelegramBot(session=session, token=settings.telegram_bot_token)
        groups = TelegramUser(settings=settings, bot_username=await bot.username())
        bridge = Bridge(store=store, vk=vk, groups=groups, bot=bot)
        await groups.connect()
        try:
            return await bridge.refresh_saved(groups_only=groups_only)
        finally:
            await groups.disconnect()
            store.close()


async def login(settings: Settings) -> None:
    user = TelegramUser(settings=settings, bot_username="")
    await user.login()


async def run(settings: Settings) -> None:
    store = Store(settings.database_path)
    credentials = resolve_credentials(
        store.read_credentials(),
        settings,
        session=read_session(VK_SESSION_PATH),
    )
    if credentials.refresh_token and not credentials.can_refresh() and not credentials.can_exchange():
        logger.warning(
            "refresh_token задан без device_id и client_id, обновление отключено"
        )
    if credentials.can_exchange():
        logger.info("продление сессии VK включено, клиент %s", credentials.client_id)
    store.save_credentials(credentials)
    async with aiohttp.ClientSession() as session:
        vk = VkClient(
            session=session,
            store=store,
            credentials=credentials,
            session_path=VK_SESSION_PATH,
        )
        bot = TelegramBot(session=session, token=settings.telegram_bot_token)
        username = await bot.username()
        groups = TelegramUser(settings=settings, bot_username=username)
        bridge = Bridge(store=store, vk=vk, groups=groups, bot=bot)
        groups.set_reply_handler(bridge.handle_reply)
        groups.set_edit_handler(bridge.handle_edit)
        groups.set_delete_handler(bridge.handle_delete)
        await groups.connect()
        pulse = asyncio.create_task(_pulse_loop(bridge))
        presence = asyncio.create_task(_presence_loop(bridge))
        reads = asyncio.create_task(_read_loop(bridge))
        logger.info("мост запущен: отдельный чат на диалог, без прокси")
        try:
            await _poll(vk=vk, store=store, bridge=bridge)
        finally:
            reads.cancel()
            pulse.cancel()
            presence.cancel()
            await groups.disconnect()
            store.close()


async def _poll(*, vk: VkClient, store: Store, bridge: Bridge) -> None:
    server = await vk.open_long_poll()
    while True:
        try:
            server = await _poll_once(vk=vk, store=store, bridge=bridge, server=server)
        except VkAuthError as exc:
            logger.error("токен VK недействителен: %s", exc)
            await bridge.report(key="vk-auth", title="токен VK", detail=exception_detail(exc))
            return
        except LongPollVersionError as exc:
            logger.error("%s", exc)
            await bridge.report(key="long-poll-version", title="long poll", detail=exception_detail(exc))
            return
        except (aiohttp.ClientError, asyncio.TimeoutError, VkApiError) as exc:
            logger.error("сбой long poll: %s", exc, exc_info=True)
            await bridge.report(key="long-poll", title="long poll", detail=exception_detail(exc))
            await asyncio.sleep(RETRY_PAUSE_SECONDS)
            server = await vk.open_long_poll()


async def _poll_once(
    *,
    vk: VkClient,
    store: Store,
    bridge: Bridge,
    server: LongPollServer,
) -> LongPollServer:
    tick = await vk.check(server)
    bridge.note_poll()
    step = long_poll_step(tick)
    if step is LongPollStep.NEW_TS and tick.ts is not None:
        logger.debug("long poll сдвинул ts на %s", tick.ts)
        store.save_cursor(tick.ts)
        return server.model_copy(update={"ts": tick.ts})
    if step is LongPollStep.NEW_TS:
        return await vk.open_long_poll()
    if step is LongPollStep.NEW_SERVER:
        logger.info("ключ long poll истёк, запрашиваю сервер заново")
        return await vk.open_long_poll()
    if step is LongPollStep.BAD_VERSION:
        raise LongPollVersionError(code=0, message="неподдерживаемая версия long poll")
    for raw in tick.updates:
        presence = parse_presence(raw)
        if presence is not None:
            user_id, online, stamp = presence
            moment = datetime.fromtimestamp(stamp) if stamp else None
            try:
                await bridge.handle_presence(user_id, online=online, at=moment)
            except Exception as exc:
                logger.exception("не удалось обновить статус %s", user_id)
                await bridge.report(
                    key=f"presence:{user_id}",
                    title="статус диалога",
                    detail=f"peer {user_id}\n{exception_detail(exc)}",
                )
            continue
        typing_peer = parse_typing(raw)
        if typing_peer is not None:
            try:
                await bridge.handle_typing(typing_peer)
            except Exception:
                logger.exception("не удалось показать набор для %s", typing_peer)
            continue
        edited = parse_message_edit(raw)
        if edited is not None:
            message_id, peer_id, _text, stamp = edited
            try:
                await bridge.handle_vk_edit(
                    peer_id=peer_id,
                    message_id=message_id,
                    at=_moment(stamp),
                )
            except Exception as exc:
                logger.exception("не удалось сохранить правку %s", message_id)
                await bridge.report(
                    key=f"vk-edit-in:{message_id}",
                    title="правка из VK",
                    detail=f"peer {peer_id}, сообщение {message_id}\n{exception_detail(exc)}",
                )
            continue
        deleted = parse_message_deleted(raw)
        if deleted is not None:
            message_id, peer_id = deleted
            try:
                await bridge.handle_vk_delete(peer_id=peer_id, message_id=message_id, at=datetime.now())
            except Exception as exc:
                logger.exception("не удалось отметить удаление %s", message_id)
                await bridge.report(
                    key=f"vk-delete-in:{message_id}",
                    title="удаление из VK",
                    detail=f"peer {peer_id}, сообщение {message_id}\n{exception_detail(exc)}",
                )
            continue
        read = parse_outgoing_read(raw)
        if read is not None:
            peer_id, up_to = read
            bridge.note_read(peer_id, up_to)
            continue
        message = parse_new_message(raw)
        if message is None or store.is_seen(message.message_id):
            continue
        await _deliver(bridge=bridge, message=message)
        store.mark_seen(message.message_id)
    if tick.ts is not None:
        store.save_cursor(tick.ts)
        return server.model_copy(update={"ts": tick.ts})
    return server


async def _read_loop(bridge: Bridge) -> None:
    while True:
        await bridge.wait_reads()
        try:
            await bridge.apply_pending_reads()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("не удалось отметить прочтение")
            await asyncio.sleep(RETRY_PAUSE_SECONDS)
            bridge.wake_reads()


async def _presence_loop(bridge: Bridge) -> None:
    while True:
        try:
            await bridge.refresh_presence()
        except Exception as exc:
            logger.exception("не удалось обновить статусы диалогов")
            await bridge.report(key="presence", title="статусы диалогов", detail=exception_detail(exc))
        await asyncio.sleep(PRESENCE_POLL_SECONDS)


async def _pulse_loop(bridge: Bridge) -> None:
    while True:
        try:
            await bridge.pulse()
        except Exception as exc:
            logger.exception("не удалось обновить статус моста")
            await bridge.report(key="pulse", title="статус моста", detail=exception_detail(exc))
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


def _moment(stamp: int | None) -> datetime:
    if stamp:
        return datetime.fromtimestamp(stamp)
    return datetime.now()


class LongPollVersionError(VkApiError):
    pass


async def _deliver(*, bridge: Bridge, message: IncomingVkMessage) -> None:
    for attempt in range(1, DELIVERY_ATTEMPTS + 1):
        try:
            await bridge.handle_vk(message)
            return
        except FloodWaitError as exc:
            notice = flood_wait_detail(exc.seconds, action="создать группу")
            extra = f"peer {message.peer_id}, сообщение {message.message_id}"
            if message.text.strip():
                extra = f"{extra}\nтекст: {brief(message.text)}"
            logger.error(
                "Telegram ограничил создание групп на %s с, сообщение %s пропущено",
                exc.seconds,
                message.message_id,
            )
            await bridge.report(
                key=f"create-group:{message.peer_id}",
                title="создание чата",
                detail=f"{notice}\n{extra}\nСообщение в Telegram не попало и само не повторится.",
            )
            return
        except Exception as exc:
            logger.exception(
                "попытка %s не доставила сообщение %s",
                attempt,
                message.message_id,
            )
            if attempt == DELIVERY_ATTEMPTS:
                logger.error("пропускаю сообщение %s", message.message_id)
                await bridge.report(
                    key=f"deliver:{message.message_id}",
                    title="доставка в Telegram",
                    detail=(
                        f"peer {message.peer_id}, сообщение {message.message_id}\n"
                        f"{exception_detail(exc)}\n"
                        "Сообщение в Telegram не попало и само не повторится."
                    ),
                )
                return
            await asyncio.sleep(RETRY_PAUSE_SECONDS)
