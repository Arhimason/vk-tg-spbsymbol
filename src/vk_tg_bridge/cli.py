import argparse
import asyncio
import logging

from vk_tg_bridge.app import login, run
from vk_tg_bridge.config import load_settings
from vk_tg_bridge.watch import run_watch

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Личные сообщения ВКонтакте в отдельные чаты Telegram",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "login", "watch"),
        help="run запускает мост, login создаёт файл сессии Telethon, watch сообщает о молчании",
    )
    args = parser.parse_args()
    settings = load_settings(login_only=args.command == "login")
    if args.command == "login":
        asyncio.run(login(settings))
        return
    if args.command == "watch":
        asyncio.run(run_watch(settings))
        return
    asyncio.run(run(settings))
