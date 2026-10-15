from datetime import datetime

from vk_tg_bridge.constants import (
    CHAT_GROUP_MARK,
    CHAT_TITLE_MARK,
    DELETED_LABEL,
    EMPTY_MESSAGE_TEXT,
    ONLINE_ABOUT,
    REVISION_LABEL,
    TELEGRAM_MESSAGE_LIMIT,
    TELEGRAM_TITLE_LIMIT,
)

_MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def format_clock(moment: datetime) -> str:
    return f"{moment.day} {_MONTHS[moment.month - 1]} {moment:%H:%M}"


def chat_title(name: str, peer_id: int, *, group: bool = False) -> str:
    cleaned = " ".join(name.split()).strip()
    if not cleaned:
        cleaned = str(peer_id)
    mark = f"{CHAT_TITLE_MARK} "
    group_mark = f"{CHAT_GROUP_MARK} "
    if cleaned.startswith(mark):
        cleaned = cleaned[len(mark):]
    if cleaned.startswith(group_mark):
        cleaned = cleaned[len(group_mark):]
    if group:
        cleaned = f"{mark}{group_mark}{cleaned}"
    else:
        cleaned = f"{mark}{cleaned}"
    return cleaned[:TELEGRAM_TITLE_LIMIT]


def title_needs_refresh(title: str, *, group: bool) -> bool:
    if group:
        return not title.startswith(f"{CHAT_TITLE_MARK} {CHAT_GROUP_MARK} ")
    return not title.startswith(f"{CHAT_TITLE_MARK} ")


def with_sender(text: str, name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        return text
    return f"{cleaned}:\n{text}" if text else f"{cleaned}:"


def append_version(current: str, new: str, moment: datetime) -> str:
    fresh = new.strip()
    if not fresh or _same_version(current, fresh):
        return current
    block = f"{_note(REVISION_LABEL, moment)}\n{fresh}"
    if not current.strip():
        updated = block
    else:
        updated = f"{current.rstrip()}\n\n{block}"
    if len(updated) <= TELEGRAM_MESSAGE_LIMIT:
        return updated
    return updated[-TELEGRAM_MESSAGE_LIMIT:]


def mark_deleted(text: str, moment: datetime) -> str:
    note = _note(DELETED_LABEL, moment)
    if _last_block(text).startswith(f"{DELETED_LABEL} · "):
        return text
    if not text.strip():
        return note
    updated = f"{text.rstrip()}\n\n{note}"
    if len(updated) <= TELEGRAM_MESSAGE_LIMIT:
        return updated
    return updated[-TELEGRAM_MESSAGE_LIMIT:]


def _note(label: str, moment: datetime) -> str:
    return f"{label} · {moment.day} {_MONTHS[moment.month - 1]} {moment:%H:%M}"


def _same_version(current: str, fresh: str) -> bool:
    block = _last_block(current)
    if not block:
        return False
    if block.startswith(f"{REVISION_LABEL} · "):
        body = block.split("\n", 1)[1].strip() if "\n" in block else ""
        return body == fresh
    return block == fresh or block.endswith(f"\n{fresh}")


def _last_block(text: str) -> str:
    if not text.strip():
        return ""
    return text.rstrip().split("\n\n")[-1]


def presence_about(*, online: bool, moment: datetime | None) -> str | None:
    if online:
        return ONLINE_ABOUT
    if moment is None:
        return None
    return f"был {moment.day} {_MONTHS[moment.month - 1]} {moment:%H:%M}"


def clip_message(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return EMPTY_MESSAGE_TEXT
    return cleaned[:TELEGRAM_MESSAGE_LIMIT]
