import json
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vk_tg_bridge.constants import GROUP_LIMIT_WAIT_SECONDS
from vk_tg_bridge.text import format_clock

_POLL_LINES = {
    "starting": "long poll запускается",
    "ok": "long poll на связи",
    "stale": "long poll не отвечает",
}
_STATUS_COMMANDS = {"/status", "статус"}


@dataclass(frozen=True)
class WatchDecision:
    action: str
    notice: str = ""


def format_status(*, now: datetime, poll_state: str, token_until: datetime | None) -> str:
    if token_until is None:
        token_line = "срок токена VK неизвестен"
    else:
        token_line = f"токен VK до {format_clock(token_until)}"
    return "\n".join(
        (
            "мост жив",
            format_clock(now),
            _POLL_LINES[poll_state],
            token_line,
        )
    )


def silence_notice(since: datetime) -> str:
    return f"мост молчит с {format_clock(since)}"


def format_incident(*, now: datetime, title: str, detail: str) -> str:
    return "\n".join((f"ошибка · {title}", format_clock(now), detail.strip()))


def flood_wait_detail(seconds: int, *, action: str) -> str:
    if seconds >= GROUP_LIMIT_WAIT_SECONDS:
        hours = max(1, round(seconds / 3600))
        return (
            f"Telegram не даёт {action}: пауза {hours} ч ({seconds} с). "
            "Обычно так выглядит лимит около 50 новых групп в сутки. "
            "Пока пауза не кончится, новые чаты не создаются."
        )
    return f"Telegram не даёт {action}: пауза {seconds} с."


def exception_detail(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".strip()
    if exc.__traceback__ is None:
        return text
    stack = "".join(traceback.format_tb(exc.__traceback__)).strip()
    if not stack:
        return text
    return f"{text}\n{stack[-1200:]}"


def brief(text: str, limit: int = 180) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}…"


class IncidentGate:
    def __init__(self, *, repeat_after: float) -> None:
        self._repeat_after = repeat_after
        self._seen: dict[str, float] = {}

    def due(self, key: str, now: float) -> bool:
        previous = self._seen.get(key)
        return previous is None or now - previous >= self._repeat_after

    def mark(self, key: str, now: float) -> None:
        self._seen[key] = now


def is_status_command(text: str) -> bool:
    parts = text.strip().lower().split()
    if len(parts) != 1:
        return False
    return parts[0].split("@", 1)[0] in _STATUS_COMMANDS


def decide_watch(
    *,
    heartbeat_at: float | None,
    now: float,
    stale_after: float,
    alerted: bool,
    chat_id: int | None,
) -> WatchDecision:
    if heartbeat_at is None or chat_id is None:
        return WatchDecision("wait")
    fresh = now - heartbeat_at < stale_after
    if fresh and alerted:
        return WatchDecision("clear")
    if fresh or alerted:
        return WatchDecision("wait")
    return WatchDecision("alert", silence_notice(datetime.fromtimestamp(heartbeat_at)))


def write_heartbeat(path: Path, *, updated_at: float, chat_id: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated_at": updated_at, "chat_id": chat_id}))


def read_heartbeat(path: Path) -> tuple[float, int] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    updated_at = payload.get("updated_at")
    chat_id = payload.get("chat_id")
    if not isinstance(updated_at, int | float) or not isinstance(chat_id, int):
        return None
    return float(updated_at), chat_id


def read_alerted(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    return isinstance(payload, dict) and payload.get("alerted") is True


def write_alerted(path: Path, alerted: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"alerted": alerted}))
