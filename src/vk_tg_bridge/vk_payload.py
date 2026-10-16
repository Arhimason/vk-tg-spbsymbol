import json
import re
from enum import Enum
from typing import Any
from urllib.parse import parse_qs, urlparse

from vk_tg_bridge.constants import (
    CHAT_PEER_BASE,
    DELETED_FLAG,
    LONG_POLL_FAILED_INFO,
    LONG_POLL_FAILED_KEY,
    LONG_POLL_FAILED_TS,
    LONG_POLL_FAILED_VERSION,
    MEDIA_ANIMATION,
    MEDIA_AUDIO,
    MEDIA_DOCUMENT,
    MEDIA_PHOTO,
    MEDIA_VIDEO,
    MEDIA_VIDEO_NOTE,
    MEDIA_VOICE,
    EDIT_MESSAGE_EVENT,
    CHAT_TYPING_EVENT,
    FRIEND_OFFLINE_EVENT,
    FRIEND_ONLINE_EVENT,
    CHAT_TYPING_MANY_EVENT,
    NEW_MESSAGE_EVENT,
    SET_FLAGS_EVENT,
    READ_OUTGOING_EVENT,
    USER_TYPING_EVENT,
    OUTBOX_FLAG,
    STICKER_FILE_NAME,
    VK_STICKER_SIZE,
    JsonKey,
)
from vk_tg_bridge.models import InboundFile, IncomingVkMessage, LongPollTick


class LongPollStep(Enum):
    EVENTS = "events"
    NEW_TS = "new_ts"
    NEW_SERVER = "new_server"
    BAD_VERSION = "bad_version"


def long_poll_step(tick: LongPollTick) -> LongPollStep:
    if tick.failed == LONG_POLL_FAILED_TS:
        return LongPollStep.NEW_TS
    if tick.failed in (LONG_POLL_FAILED_KEY, LONG_POLL_FAILED_INFO):
        return LongPollStep.NEW_SERVER
    if tick.failed == LONG_POLL_FAILED_VERSION:
        return LongPollStep.BAD_VERSION
    return LongPollStep.EVENTS


def parse_message_edit(update: Any) -> tuple[int, int, str, int | None] | None:
    if not isinstance(update, list) or len(update) < 4 or update[0] != EDIT_MESSAGE_EVENT:
        return None
    text = update[5] if len(update) > 5 and isinstance(update[5], str) else ""
    stamp = update[4] if len(update) > 4 and isinstance(update[4], int) and update[4] > 0 else None
    return int(update[1]), int(update[3]), text, stamp


def parse_message_deleted(update: Any) -> tuple[int, int] | None:
    if not isinstance(update, list) or len(update) < 4 or update[0] != SET_FLAGS_EVENT:
        return None
    if not int(update[2]) & DELETED_FLAG:
        return None
    return int(update[1]), int(update[3])


def attachment_tokens(raw: dict[str, Any]) -> str:
    tokens: list[str] = []
    attachments = raw.get(JsonKey.ATTACHMENTS) or []
    if not isinstance(attachments, list):
        return ""
    for item in attachments:
        if not isinstance(item, dict):
            continue
        kind = item.get(JsonKey.TYPE)
        body = item.get(kind) if isinstance(kind, str) else None
        if not isinstance(body, dict):
            continue
        owner_id = body.get(JsonKey.OWNER_ID)
        media_id = body.get(JsonKey.ID)
        if owner_id is None or media_id is None:
            continue
        token = f"{kind}{owner_id}_{media_id}"
        access_key = body.get(JsonKey.ACCESS_KEY)
        if isinstance(access_key, str) and access_key:
            token = f"{token}_{access_key}"
        tokens.append(token)
    return ",".join(tokens)


def parse_presence(update: Any) -> tuple[int, bool, int | None] | None:
    if not isinstance(update, list) or len(update) < 2 or not isinstance(update[1], int):
        return None
    if update[0] not in (FRIEND_ONLINE_EVENT, FRIEND_OFFLINE_EVENT) or update[1] == 0:
        return None
    stamp = update[3] if len(update) > 3 and isinstance(update[3], int) and update[3] > 0 else None
    return abs(update[1]), update[0] == FRIEND_ONLINE_EVENT, stamp


def presence_from_user(payload: Any) -> tuple[bool, int | None] | None:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return None
    user = payload[0]
    if user.get(JsonKey.ONLINE) == 1 or user.get(JsonKey.ONLINE_MOBILE) == 1:
        return True, None
    last_seen = user.get(JsonKey.LAST_SEEN)
    if isinstance(last_seen, dict):
        stamp = last_seen.get(JsonKey.TIME)
        if isinstance(stamp, int) and stamp > 0:
            return False, stamp
    return False, None


_VK_HOSTS = {"vk.com", "vk.ru", "m.vk.com", "m.vk.ru"}
_SCREEN_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.]*")


def add_command_targets(text: str) -> list[str] | None:
    parts = text.strip().split()
    if not parts:
        return None
    head = parts[0].split("@", 1)[0].lower()
    if head != "/a":
        return None
    return parts[1:]


def page_target(token: str) -> int | str | None:
    raw = token.strip().strip("<>").rstrip(".,);")
    if not raw:
        return None
    lowered = raw.lower()
    if "://" not in lowered and not lowered.startswith(("vk.com/", "vk.ru/", "m.vk.com/", "m.vk.ru/")):
        return _page_slug(raw)
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    host = parsed.netloc.lower().removeprefix("www.")
    if host not in _VK_HOSTS:
        return None
    selected = parse_qs(parsed.query).get("sel", [None])[0]
    if isinstance(selected, str) and selected.lstrip("-").isdigit() and selected not in {"", "-"}:
        return int(selected)
    slug = parsed.path.strip("/").split("/", 1)[0]
    return _page_slug(slug)


def _page_slug(slug: str) -> int | str | None:
    if slug.startswith("id") and slug[2:].isdigit() and int(slug[2:]) > 0:
        return int(slug[2:])
    for prefix in ("club", "public", "event"):
        number = slug[len(prefix):]
        if slug.startswith(prefix) and number.isdigit() and int(number) > 0:
            return -int(number)
    if _SCREEN_NAME.fullmatch(slug):
        return slug
    return None


def peer_from_resolved(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    object_id = payload.get(JsonKey.OBJECT_ID)
    if not isinstance(object_id, int) or object_id <= 0:
        return None
    kind = payload.get(JsonKey.TYPE)
    if kind == "user":
        return object_id
    if kind in {"group", "page", "event"}:
        return -object_id
    return None


def parse_typing(update: Any) -> int | None:
    if not isinstance(update, list) or len(update) < 3:
        return None
    code = update[0]
    if code == USER_TYPING_EVENT and isinstance(update[1], int):
        return update[1]
    if code == CHAT_TYPING_EVENT and isinstance(update[2], int):
        chat_id = update[2]
        if chat_id >= CHAT_PEER_BASE:
            return chat_id
        return CHAT_PEER_BASE + chat_id
    if code == CHAT_TYPING_MANY_EVENT and isinstance(update[2], int):
        return update[2]
    return None


def parse_outgoing_read(update: Any) -> tuple[int, int] | None:
    if not isinstance(update, list) or len(update) < 3:
        return None
    if update[0] != READ_OUTGOING_EVENT:
        return None
    return int(update[1]), int(update[2])


def parse_new_message(update: Any) -> IncomingVkMessage | None:
    if not isinstance(update, list) or not update:
        return None
    if update[0] != NEW_MESSAGE_EVENT or len(update) < 4:
        return None
    flags = int(update[2])
    if flags & OUTBOX_FLAG or flags & DELETED_FLAG:
        return None
    text = ""
    if len(update) > 5 and isinstance(update[5], str):
        text = update[5]
    return IncomingVkMessage(
        message_id=int(update[1]),
        peer_id=int(update[3]),
        text=text,
    )


def largest_photo_url(photo: dict[str, Any]) -> str | None:
    best_url: str | None = None
    best_width = -1
    sizes = photo.get(JsonKey.SIZES) or []
    if isinstance(sizes, list):
        for size in sizes:
            if not isinstance(size, dict):
                continue
            url = size.get(JsonKey.URL)
            if not isinstance(url, str) or not url:
                continue
            width = int(size.get(JsonKey.WIDTH) or 0)
            if width >= best_width:
                best_url = url
                best_width = width
    if best_url is not None:
        return best_url
    original = photo.get(JsonKey.ORIG_PHOTO) or {}
    if isinstance(original, dict):
        url = original.get(JsonKey.URL)
        if isinstance(url, str) and url:
            return url
    return None


_ATTACHMENT_NOTES = {
    "doc": "файл",
    "audio": "аудио",
    "video": "видео",
    "clip": "клип",
    "sticker": "стикер",
    "gift": "подарок",
    "call": "звонок",
    "story": "история",
    "market": "товар",
    "wall": "запись",
    "wall_reply": "комментарий",
    "link": "ссылка",
    "poll": "опрос",
    "graffiti": "граффити",
    "audio_message": "голосовое",
    "podcast": "подкаст",
    "article": "статья",
    "mini_app": "приложение",
    "photo": "фото",
}

_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
_ANIMATION_EXTENSIONS = {"gif"}
_VIDEO_EXTENSIONS = {"mp4", "mov", "mkv", "webm"}
_AUDIO_EXTENSIONS = {"mp3", "m4a", "flac", "wav"}
_VOICE_EXTENSIONS = {"ogg", "opus"}


def conversation_message_id(raw: dict[str, Any]) -> int | None:
    value = raw.get("conversation_message_id")
    if isinstance(value, int):
        return value
    return None


def reply_conversation_id(raw: dict[str, Any]) -> int | None:
    reply = raw.get("reply_message")
    if not isinstance(reply, dict):
        return None
    return conversation_message_id(reply)


def vk_reply_forward(*, peer_id: int, conversation_message_id: int) -> str:
    return json.dumps(
        {
            "peer_id": peer_id,
            "conversation_message_ids": [conversation_message_id],
            "is_reply": True,
        },
        separators=(",", ":"),
    )


def message_body(raw: dict[str, Any], *, nested: bool = False) -> tuple[str, list[InboundFile]]:
    text = _plain_text(raw.get(JsonKey.TEXT))
    files, notes = _attachments(raw.get(JsonKey.ATTACHMENTS))
    if not nested:
        forwarded = raw.get("fwd_messages")
        if isinstance(forwarded, list):
            for item in forwarded:
                if not isinstance(item, dict):
                    continue
                forwarded_text, forwarded_files = message_body(item, nested=True)
                notes.append(f"Переслано: {forwarded_text}" if forwarded_text else "Переслано")
                files.extend(forwarded_files)
    if notes:
        text = "\n".join(part for part in (text, *notes) if part)
    return text.strip(), files


def saved_video_message_attachment(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("VK не вернул видеосообщение")
    owner_id = payload.get(JsonKey.OWNER_ID)
    video_id = payload.get("video_id")
    if video_id is None:
        video_id = payload.get(JsonKey.ID)
    if owner_id is None or video_id is None:
        raise ValueError("у видеосообщения VK нет id")
    return f"video_message{owner_id}_{video_id}"


def saved_video_attachment(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("VK не вернул видео")
    owner_id = payload.get(JsonKey.OWNER_ID)
    video_id = payload.get("video_id")
    if video_id is None:
        video_id = payload.get(JsonKey.ID)
    if owner_id is None or video_id is None:
        raise ValueError("у видео VK нет id")
    attachment = f"video{owner_id}_{video_id}"
    access_key = payload.get(JsonKey.ACCESS_KEY)
    if isinstance(access_key, str) and access_key:
        attachment = f"{attachment}_{access_key}"
    return attachment


def title_from_users(payload: Any) -> str:
    if not isinstance(payload, list) or not payload:
        return ""
    user = payload[0]
    if not isinstance(user, dict):
        return ""
    first = user.get(JsonKey.FIRST_NAME) or ""
    last = user.get(JsonKey.LAST_NAME) or ""
    return f"{first} {last}".strip()


def title_from_groups(payload: Any) -> str:
    groups = payload
    if isinstance(payload, dict):
        groups = payload.get(JsonKey.GROUPS) or payload.get(JsonKey.ITEMS) or []
    if not isinstance(groups, list) or not groups:
        return ""
    group = groups[0]
    if not isinstance(group, dict):
        return ""
    name = group.get(JsonKey.NAME) or ""
    return name.strip() if isinstance(name, str) else ""


def title_from_chat(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    items = payload.get(JsonKey.ITEMS) or []
    if not isinstance(items, list) or not items:
        return ""
    item = items[0]
    if not isinstance(item, dict):
        return ""
    settings = item.get(JsonKey.CHAT_SETTINGS) or {}
    if not isinstance(settings, dict):
        return ""
    title = settings.get(JsonKey.TITLE) or ""
    return title.strip() if isinstance(title, str) else ""


def is_chat_peer(peer_id: int) -> bool:
    return peer_id >= CHAT_PEER_BASE


_AVATAR_FIELDS = ("photo_max_orig", "photo_400", "photo_200", "photo_100")
_PLACEHOLDER_MARKS = ("camera_", "community_", "/images/camera", "/images/community")


def avatar_url_from_user(payload: Any) -> str | None:
    user = payload[0] if isinstance(payload, list) and payload else None
    if not isinstance(user, dict) or not user.get("has_photo"):
        return None
    return _avatar_url(user)


def avatar_url_from_group(payload: Any) -> str | None:
    groups = payload
    if isinstance(payload, dict):
        groups = payload.get(JsonKey.GROUPS) or payload.get(JsonKey.ITEMS) or []
    group = groups[0] if isinstance(groups, list) and groups else None
    if not isinstance(group, dict):
        return None
    return _avatar_url(group)


def avatar_url_from_chat(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    items = payload.get(JsonKey.ITEMS) or []
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    settings = items[0].get(JsonKey.CHAT_SETTINGS) or {}
    if not isinstance(settings, dict):
        return None
    photo = settings.get(JsonKey.PHOTO)
    if not isinstance(photo, dict) or photo.get("is_default_photo"):
        return None
    return _avatar_url(photo)


def _avatar_url(item: dict[str, Any]) -> str | None:
    for field in _AVATAR_FIELDS:
        url = item.get(field)
        if isinstance(url, str) and url.startswith("http") and not _placeholder_avatar(url):
            return url
    return None


def _placeholder_avatar(url: str) -> bool:
    lowered = url.lower()
    return any(mark in lowered for mark in _PLACEHOLDER_MARKS)


def _plain_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _attachments(raw: Any) -> tuple[list[InboundFile], list[str]]:
    if not isinstance(raw, list):
        return [], []
    files: list[InboundFile] = []
    notes: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        found, note = _attachment(item)
        files.extend(found)
        if note:
            notes.append(note)
    return files, notes


def _attachment(item: dict[str, Any]) -> tuple[list[InboundFile], str]:
    kind = item.get(JsonKey.TYPE)
    body = item.get(kind) if isinstance(kind, str) else None
    if not isinstance(body, dict):
        body = {}
    if kind == JsonKey.PHOTO:
        return _or_note(_photo_file(body), "фото")
    if kind == "graffiti":
        return _or_note(_url_file(body.get(JsonKey.URL), kind=MEDIA_PHOTO, filename="graffiti.png"), "граффити")
    if kind == "sticker":
        return _or_note(_sticker_file(body), "стикер")
    if kind == JsonKey.AUDIO_MESSAGE:
        return _or_note(_voice_file(body), "голосовое")
    if kind == JsonKey.DOC:
        return _or_note(_doc_file(body), "файл")
    if kind in {"video", "clip", "video_message"}:
        files = _video_file(body)
        circle = kind == "video_message" or body.get(JsonKey.TYPE) == "video_message"
        if circle:
            files = [
                item.model_copy(update={"kind": MEDIA_VIDEO_NOTE, "filename": "video_note.mp4"})
                for item in files
            ]
        if files:
            return files, ""
        fallback = "видеосообщение" if circle else "клип" if kind == "clip" else "видео"
        note = _video_note(body, fallback=fallback)
        return [], note if note != fallback else f"[{fallback}]"
    if kind == "audio":
        files = _audio_file(body)
        if files:
            return files, ""
        title = _audio_title(body)
        return [], title if title != "audio" else "[аудио]"
    if kind == "link":
        return [], _link_note(body)
    if kind in {"wall", "wall_reply"}:
        return _wall(body, comment=kind == "wall_reply")
    if kind == "poll":
        return [], _poll_note(body)
    if kind == "market":
        return [], _market_note(body)
    if isinstance(kind, str) and kind:
        return [], f"[{_ATTACHMENT_NOTES.get(kind, kind)}]"
    return [], ""


def _or_note(files: list[InboundFile], note: str) -> tuple[list[InboundFile], str]:
    return (files, "") if files else ([], f"[{note}]")


def _photo_file(photo: dict[str, Any]) -> list[InboundFile]:
    url = largest_photo_url(photo)
    if url is None:
        return []
    return [InboundFile(kind=MEDIA_PHOTO, url=url, filename="photo.jpg")]


def _voice_file(body: dict[str, Any]) -> list[InboundFile]:
    ogg = body.get("link_ogg")
    if isinstance(ogg, str) and ogg:
        return [InboundFile(kind=MEDIA_VOICE, url=ogg, filename="voice.ogg")]
    mp3 = body.get("link_mp3")
    if isinstance(mp3, str) and mp3:
        return [InboundFile(kind=MEDIA_AUDIO, url=mp3, filename="voice.mp3")]
    return []


def _doc_file(body: dict[str, Any]) -> list[InboundFile]:
    url = body.get(JsonKey.URL)
    if not isinstance(url, str) or not url:
        return []
    ext = body.get("ext") if isinstance(body.get("ext"), str) else ""
    title = body.get(JsonKey.TITLE) if isinstance(body.get(JsonKey.TITLE), str) else ""
    return [
        InboundFile(
            kind=_kind_for_extension(ext),
            url=url,
            filename=_safe_filename(title or "file", ext),
        )
    ]


def _video_file(body: dict[str, Any]) -> list[InboundFile]:
    url = _best_mp4(body.get("files"))
    if not url:
        return []
    title = body.get(JsonKey.TITLE) if isinstance(body.get(JsonKey.TITLE), str) else ""
    return [InboundFile(kind=MEDIA_VIDEO, url=url, filename=_safe_filename(title or "video", "mp4"))]


def _video_note(body: dict[str, Any], *, fallback: str) -> str:
    title = _plain_text(body.get(JsonKey.TITLE))
    player = _plain_text(body.get("player"))
    return " ".join(part for part in (title, player) if part) or fallback


def _audio_file(body: dict[str, Any]) -> list[InboundFile]:
    url = body.get(JsonKey.URL)
    if not isinstance(url, str) or not url:
        return []
    return [InboundFile(kind=MEDIA_AUDIO, url=url, filename=_safe_filename(_audio_title(body), "mp3"))]


def _sticker_file(body: dict[str, Any]) -> list[InboundFile]:
    animation = body.get("animation_url")
    if isinstance(animation, str) and animation:
        return [InboundFile(kind=MEDIA_DOCUMENT, url=animation, filename="sticker.webm")]
    animations = body.get("animations")
    if isinstance(animations, list):
        for item in animations:
            if not isinstance(item, dict):
                continue
            url = item.get(JsonKey.URL)
            if isinstance(url, str) and url:
                return [InboundFile(kind=MEDIA_DOCUMENT, url=url, filename="sticker.webm")]
    url = _widest_url(body.get("images")) or _widest_url(body.get("images_with_background"))
    if url:
        return [InboundFile(kind=MEDIA_PHOTO, url=url, filename=STICKER_FILE_NAME)]
    sticker_id = body.get("sticker_id")
    if isinstance(sticker_id, int) and sticker_id > 0:
        return [
            InboundFile(
                kind=MEDIA_PHOTO,
                url=f"https://vk.com/sticker/1-{sticker_id}-{VK_STICKER_SIZE}",
                filename=STICKER_FILE_NAME,
            )
        ]
    return []


def _url_file(url: Any, *, kind: str, filename: str) -> list[InboundFile]:
    if not isinstance(url, str) or not url:
        return []
    return [InboundFile(kind=kind, url=url, filename=filename)]


def _wall(body: dict[str, Any], *, comment: bool) -> tuple[list[InboundFile], str]:
    files, notes = _attachments(body.get(JsonKey.ATTACHMENTS))
    label = "Комментарий" if comment else "Запись"
    text = _plain_text(body.get(JsonKey.TEXT))
    note = f"{label}: {text}" if text else label
    if notes:
        note = "\n".join((note, *notes))
    return files, note


def _link_note(body: dict[str, Any]) -> str:
    parts = [
        _plain_text(body.get(JsonKey.TITLE)),
        _plain_text(body.get("description")),
        _plain_text(body.get(JsonKey.URL)),
    ]
    return " — ".join(part for part in parts if part) or "ссылка"


def _poll_note(body: dict[str, Any]) -> str:
    question = _plain_text(body.get("question"))
    lines = [f"Опрос: {question}" if question else "Опрос"]
    answers = body.get("answers")
    if isinstance(answers, list):
        for answer in answers[:10]:
            if isinstance(answer, dict):
                text = _plain_text(answer.get(JsonKey.TEXT))
                if text:
                    lines.append(f"• {text}")
    return "\n".join(lines)


def _market_note(body: dict[str, Any]) -> str:
    price = body.get("price")
    price_text = _plain_text(price.get(JsonKey.TEXT)) if isinstance(price, dict) else ""
    parts = [
        _plain_text(body.get(JsonKey.TITLE)),
        price_text,
        _plain_text(body.get(JsonKey.URL)),
    ]
    return " ".join(part for part in parts if part) or "товар"


def _audio_title(body: dict[str, Any]) -> str:
    artist = _plain_text(body.get("artist"))
    title = _plain_text(body.get(JsonKey.TITLE))
    return " — ".join(part for part in (artist, title) if part) or "audio"


def _best_mp4(files: Any) -> str:
    if not isinstance(files, dict):
        return ""
    best = ""
    best_height = -1
    for key, url in files.items():
        if not isinstance(key, str) or not key.startswith("mp4_") or not isinstance(url, str) or not url:
            continue
        height_text = key.removeprefix("mp4_")
        height = int(height_text) if height_text.isdigit() else 0
        if height >= best_height:
            best = url
            best_height = height
    return best


def _widest_url(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    best = ""
    best_width = -1
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get(JsonKey.URL)
        if not isinstance(url, str) or not url:
            continue
        width = int(item.get(JsonKey.WIDTH) or 0)
        if width >= best_width:
            best = url
            best_width = width
    return best


def _kind_for_extension(ext: str) -> str:
    lowered = ext.lower()
    if lowered in _IMAGE_EXTENSIONS:
        return MEDIA_PHOTO
    if lowered in _ANIMATION_EXTENSIONS:
        return MEDIA_ANIMATION
    if lowered in _VIDEO_EXTENSIONS:
        return MEDIA_VIDEO
    if lowered in _AUDIO_EXTENSIONS:
        return MEDIA_AUDIO
    if lowered in _VOICE_EXTENSIONS:
        return MEDIA_VOICE
    return MEDIA_DOCUMENT


def _safe_filename(name: str, ext: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._- " else "_" for char in name).strip(" .")
    if not cleaned:
        cleaned = "file"
    suffix = f".{ext.lstrip('.')}" if ext else ""
    if suffix and not cleaned.lower().endswith(suffix.lower()):
        cleaned = f"{cleaned}{suffix}"
    return cleaned[:120]


def saved_photo_attachment(payload: Any) -> str:
    photo = payload[0] if isinstance(payload, list) else payload
    return _media_attachment(kind=JsonKey.PHOTO, item=photo)


def saved_voice_attachment(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("VK не вернул голосовое")
    audio = payload.get(JsonKey.AUDIO_MESSAGE)
    if isinstance(audio, dict):
        return _media_attachment(kind=JsonKey.AUDIO_MESSAGE, item=audio)
    document = payload.get(JsonKey.DOC)
    if isinstance(document, dict):
        return _media_attachment(kind=JsonKey.DOC, item=document)
    kind = JsonKey.AUDIO_MESSAGE if payload.get(JsonKey.TYPE) == JsonKey.AUDIO_MESSAGE else JsonKey.DOC
    return _media_attachment(kind=kind, item=payload)


def _media_attachment(*, kind: str, item: Any) -> str:
    if not isinstance(item, dict):
        raise ValueError("VK не вернул вложение")
    owner_id = item.get(JsonKey.OWNER_ID)
    media_id = item.get(JsonKey.ID)
    if owner_id is None or media_id is None:
        raise ValueError("у вложения VK нет id")
    attachment = f"{kind}{owner_id}_{media_id}"
    access_key = item.get(JsonKey.ACCESS_KEY)
    if isinstance(access_key, str) and access_key:
        attachment = f"{attachment}_{access_key}"
    return attachment
