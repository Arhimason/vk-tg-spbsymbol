import asyncio
import logging
import time
from pathlib import Path

import aiohttp

from vk_tg_bridge.constants import (
    HEARTBEAT_FILE,
    HEARTBEAT_STALE_SECONDS,
    WATCH_INTERVAL_SECONDS,
    WATCHDOG_FILE,
)
from vk_tg_bridge.health import decide_watch, read_alerted, read_heartbeat, write_alerted
from vk_tg_bridge.models import Settings
from vk_tg_bridge.telegram_bot import TelegramBot, TelegramBotError

logger = logging.getLogger(__name__)


async def run_watch(settings: Settings) -> None:
    root = Path(settings.database_path).parent
    heartbeat_path = root / HEARTBEAT_FILE
    state_path = root / WATCHDOG_FILE
    async with aiohttp.ClientSession() as session:
        bot = TelegramBot(session=session, token=settings.telegram_bot_token)
        while True:
            await _watch_once(
                bot=bot,
                heartbeat_path=heartbeat_path,
                state_path=state_path,
            )
            await asyncio.sleep(WATCH_INTERVAL_SECONDS)


async def _watch_once(
    *,
    bot: TelegramBot,
    heartbeat_path: Path,
    state_path: Path,
) -> None:
    beat = read_heartbeat(heartbeat_path)
    heartbeat_at = None if beat is None else beat[0]
    chat_id = None if beat is None else beat[1]
    decision = decide_watch(
        heartbeat_at=heartbeat_at,
        now=time.time(),
        stale_after=HEARTBEAT_STALE_SECONDS,
        alerted=read_alerted(state_path),
        chat_id=chat_id,
    )
    if decision.action == "clear":
        write_alerted(state_path, False)
        return
    if decision.action != "alert" or chat_id is None:
        return
    try:
        await bot.send_text(chat_id=chat_id, text=decision.notice)
    except TelegramBotError as exc:
        logger.error("не удалось сообщить о молчании моста: %s", exc)
        return
    write_alerted(state_path, True)
    logger.info("отправлено сообщение о молчании моста")
