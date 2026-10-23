import asyncio
import json
from datetime import datetime

import aiohttp

from telethon.errors import FloodWaitError

from vk_tg_bridge.app import _deliver
from vk_tg_bridge.bridge import Bridge
from vk_tg_bridge.constants import (
    DELIVERED_REACTION,
    READ_REACTION,
    SERVICE_CHAT_KEY,
    SERVICE_MESSAGE_KEY,
)
from vk_tg_bridge.models import InboundFile, IncomingVkMessage, LoadedMessage, OutboundFile, SentVkMessage
from vk_tg_bridge.store import Store
from vk_tg_bridge.text import chat_title
from vk_tg_bridge.vk_client import VkApiError


class FakeVk:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, tuple[tuple[str, str, bytes], ...]]] = []
        self.reply_targets: list[int | None] = []
        self.messages: dict[int, LoadedMessage] = {}
        self.edits: list[tuple[int, int, int, str]] = []
        self.deletes: list[tuple[int, int]] = []
        self.presence: dict[int, tuple[bool, int | None] | None] = {}
        self.expires_at: float | None = None
        self.screens: dict[str, int] = {}
        self.fail_reply = False
        self.fail_send = False

    async def title_for(self, peer_id: int) -> str:
        return "Анна Иванова" if peer_id == 42 else ""

    async def avatar_for(self, peer_id: int) -> bytes | None:
        return b"avatar" if peer_id == 42 else None

    async def load_message(self, message_id: int) -> LoadedMessage:
        saved = self.messages.get(message_id)
        if saved is not None:
            return saved
        return LoadedMessage(
            text=f"текст {message_id}",
            files=[InboundFile(kind="photo", url="https://vk.test/a.jpg", filename="photo.jpg")],
            from_id=99,
        )

    async def sender_name(self, user_id: int | None) -> str:
        return "Пётр" if user_id else ""

    async def send_text(self, *, peer_id: int, text: str) -> None:
        await self.send_reply(peer_id=peer_id, text=text, files=[])

    async def send_reply(
        self,
        *,
        peer_id: int,
        text: str,
        files: list[OutboundFile],
        reply_to: int | None = None,
    ) -> SentVkMessage:
        if self.fail_send:
            raise VkApiError(code=10, message="не принято")
        if reply_to is not None and self.fail_reply:
            self.fail_reply = False
            raise VkApiError(code=100, message="reply")
        self.sent.append(
            (peer_id, text, tuple((item.kind, item.filename, item.content) for item in files))
        )
        self.reply_targets.append(reply_to)
        number = len(self.sent)
        return SentVkMessage(message_id=1000 + number, conversation_message_id=2000 + number)

    async def edit_message(
        self,
        *,
        peer_id: int,
        message_id: int,
        conversation_message_id: int,
        text: str,
    ) -> None:
        self.edits.append((peer_id, message_id, conversation_message_id, text))

    async def delete_message(self, *, peer_id: int, conversation_message_id: int) -> None:
        self.deletes.append((peer_id, conversation_message_id))

    async def presence_for(self, user_id: int) -> tuple[bool, int | None] | None:
        return self.presence.get(user_id)

    async def presence_for_many(self, user_ids: list[int]) -> dict[int, tuple[bool, int | None]]:
        return {
            user_id: value
            for user_id in user_ids
            if (value := self.presence.get(user_id)) is not None
        }

    def token_expires_at(self) -> float | None:
        return self.expires_at

    async def peer_by_screen_name(self, screen_name: str) -> int | None:
        return self.screens.get(screen_name)


class FakeGroups:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.photos: list[bytes | None] = []
        self.refreshed: list[tuple[int, str, bytes | None]] = []
        self.invited: list[int] = []
        self.reactions_allowed: list[int] = []
        self.reactions: list[tuple[int, int, str]] = []
        self.reaction_attempts: list[tuple[int, int]] = []
        self.fail_react = False
        self.abouts: list[tuple[int, str]] = []
        self.service_opened = 0
        self.pins: list[tuple[int, int]] = []
        self.missing_chats: set[int] = set()
        self.flood_seconds = 0

    async def allow_reactions(self, chat_id: int) -> None:
        self.reactions_allowed.append(chat_id)

    async def react(self, *, chat_id: int, message_id: int, emoji: str = DELIVERED_REACTION) -> None:
        self.reaction_attempts.append((chat_id, message_id))
        if self.fail_react:
            raise RuntimeError("reaction")
        self.reactions.append((chat_id, message_id, emoji))

    async def create_group(self, title: str, photo: bytes | None = None) -> int:
        if self.flood_seconds:
            raise FloodWaitError(None, self.flood_seconds)
        self.created.append(title)
        self.photos.append(photo)
        return -100500

    async def refresh_group(self, chat_id: int, *, title: str, photo: bytes | None) -> None:
        self.refreshed.append((chat_id, title, photo))

    async def set_about(self, chat_id: int, about: str) -> None:
        self.abouts.append((chat_id, about))

    async def invite_bot(self, chat_id: int) -> None:
        self.invited.append(chat_id)

    async def open_service_chat(self) -> int:
        self.service_opened += 1
        return -100777

    async def has_chat(self, chat_id: int) -> bool:
        return chat_id not in self.missing_chats

    async def pin(self, chat_id: int, message_id: int) -> None:
        self.pins.append((chat_id, message_id))


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, tuple[str, ...]]] = []
        self.reply_targets: list[int | None] = []
        self.revised: list[tuple[int, int, str]] = []
        self.typing: list[int] = []
        self.texts: list[tuple[int, str]] = []
        self._next_message_id = 500

    async def send_text(self, *, chat_id: int, text: str) -> int:
        self.texts.append((chat_id, text))
        self._next_message_id += 1
        return self._next_message_id

    async def show_typing(self, chat_id: int) -> None:
        self.typing.append(chat_id)

    async def revise(self, *, chat_id: int, message_id: int, text: str) -> None:
        self.revised.append((chat_id, message_id, text))

    async def deliver(
        self,
        *,
        chat_id: int,
        text: str,
        files: list[InboundFile],
        reply_to_message_id: int | None = None,
    ) -> list[int]:
        self.sent.append((chat_id, text, tuple(item.url for item in files)))
        self.reply_targets.append(reply_to_message_id)
        pieces = (1 if text else 0) + len(files)
        ids = list(range(self._next_message_id, self._next_message_id + pieces))
        self._next_message_id += max(pieces, 1)
        return ids


def _bridge(tmp_path):
    store = Store(str(tmp_path / "bridge.db"))
    vk = FakeVk()
    groups = FakeGroups()
    bot = FakeBot()
    return Bridge(store=store, vk=vk, groups=groups, bot=bot), store, vk, groups, bot


def test_first_message_opens_one_chat(tmp_path) -> None:
    bridge, _store, _vk, groups, bot = _bridge(tmp_path)
    message = IncomingVkMessage(message_id=1, peer_id=42, text="черновик")
    asyncio.run(bridge.handle_vk(message))
    asyncio.run(bridge.handle_vk(message.model_copy(update={"message_id": 2})))
    assert groups.created == [chat_title("Анна Иванова", 42)]
    assert groups.photos == [b"avatar"]
    assert groups.invited == [-100500]
    assert len(bot.sent) == 2
    assert bot.sent[0][0] == -100500
    assert bot.sent[0][1] == "текст 1"
    assert bot.sent[0][2] == ("https://vk.test/a.jpg",)


def test_group_chat_names_the_sender_and_uses_two_prefixes(tmp_path) -> None:
    bridge, store, _vk, groups, bot = _bridge(tmp_path)
    peer_id = 2_000_000_001
    store.save_peer(vk_peer_id=peer_id, title="[VK] Друзья", telegram_chat_id=-1009, bot_invited=True)
    asyncio.run(bridge.handle_vk(IncomingVkMessage(message_id=1, peer_id=peer_id, text="привет")))
    assert groups.refreshed == [(-1009, "[VK] [Группа] 2000000001", None)]
    assert bot.sent[0][1] == "Пётр:\nтекст 1"
    saved = store.peer_by_vk(peer_id)
    assert saved is not None
    assert saved.title == "[VK] [Группа] 2000000001"


def test_existing_chat_gets_vk_title_and_avatar(tmp_path) -> None:
    bridge, store, _vk, groups, _bot = _bridge(tmp_path)
    store.save_peer(vk_peer_id=42, title="Анна", telegram_chat_id=-100500, bot_invited=True)
    asyncio.run(bridge.handle_vk(IncomingVkMessage(message_id=1, peer_id=42, text="привет")))
    assert groups.refreshed == [(-100500, "[VK] Анна Иванова", b"avatar")]
    saved = store.peer_by_vk(42)
    assert saved is not None
    assert saved.title == "[VK] Анна Иванова"
    asyncio.run(bridge.handle_vk(IncomingVkMessage(message_id=2, peer_id=42, text="ещё")))
    assert len(groups.refreshed) == 1


def test_refresh_saved_updates_every_existing_chat(tmp_path) -> None:
    bridge, store, _vk, groups, _bot = _bridge(tmp_path)
    store.save_peer(vk_peer_id=42, title="Анна", telegram_chat_id=-100500, bot_invited=True)
    store.save_peer(vk_peer_id=7, title="[VK] Чат", telegram_chat_id=-1007, bot_invited=True)
    updated = asyncio.run(bridge.refresh_saved())
    assert updated == 2
    assert groups.refreshed == [
        (-1007, "[VK] 7", None),
        (-100500, "[VK] Анна Иванова", b"avatar"),
    ]


def test_reply_goes_back_to_the_same_peer(tmp_path) -> None:
    bridge, _store, vk, _groups, _bot = _bridge(tmp_path)
    asyncio.run(bridge.handle_vk(IncomingVkMessage(message_id=1, peer_id=42, text="привет")))
    asyncio.run(bridge.handle_reply(chat_id=-100500, text="ответ"))
    asyncio.run(bridge.handle_reply(chat_id=-100999, text="чужой чат"))
    asyncio.run(bridge.handle_reply(chat_id=-100500, text="   "))
    assert vk.sent == [(42, "ответ", ())]


def test_photo_and_voice_from_telegram_go_to_vk(tmp_path) -> None:
    bridge, _store, vk, _groups, _bot = _bridge(tmp_path)
    asyncio.run(bridge.handle_vk(IncomingVkMessage(message_id=1, peer_id=42, text="привет")))
    asyncio.run(
        bridge.handle_reply(
            chat_id=-100500,
            text="смотри",
            files=[
                OutboundFile(kind="photo", filename="photo.jpg", content=b"jpeg"),
                OutboundFile(kind="voice", filename="voice.ogg", content=b"ogg"),
            ],
        )
    )
    asyncio.run(
        bridge.handle_reply(
            chat_id=-100500,
            text="",
            files=[OutboundFile(kind="video", filename="video_note.mp4", content=b"mp4")],
        )
    )
    asyncio.run(
        bridge.handle_reply(
            chat_id=-100500,
            text="",
            files=[OutboundFile(kind="document", filename="sticker.webp", content=b"webp")],
        )
    )
    assert vk.sent == [
        (
            42,
            "смотри",
            (("photo", "photo.jpg", b"jpeg"), ("voice", "voice.ogg", b"ogg")),
        ),
        (42, "", (("video", "video_note.mp4", b"mp4"),)),
        (42, "", (("document", "sticker.webp", b"webp"),)),
    ]


def test_vk_message_links_every_telegram_part(tmp_path) -> None:
    bridge, store, vk, _groups, _bot = _bridge(tmp_path)
    vk.messages[1] = LoadedMessage(
        text="текст 1",
        files=[InboundFile(kind="photo", url="https://vk.test/a.jpg", filename="photo.jpg")],
        conversation_message_id=15,
    )
    asyncio.run(bridge.handle_vk(IncomingVkMessage(message_id=1, peer_id=42, text="черновик")))
    first = store.link_by_vk(peer_id=42, conversation_message_id=15)
    assert first is not None
    assert first.vk_message_id == 1
    second = store.link_by_telegram(chat_id=-100500, message_id=first.telegram_message_id + 1)
    assert second is not None
    assert second.vk_conversation_message_id == 15


def test_vk_reply_points_at_the_saved_telegram_message(tmp_path) -> None:
    bridge, store, vk, _groups, bot = _bridge(tmp_path)
    vk.messages[1] = LoadedMessage(text="исходное", conversation_message_id=15)
    vk.messages[2] = LoadedMessage(text="ответ", conversation_message_id=16, reply_conversation_message_id=15)
    asyncio.run(bridge.handle_vk(IncomingVkMessage(message_id=1, peer_id=42, text="исходное")))
    origin = store.link_by_vk(peer_id=42, conversation_message_id=15)
    assert origin is not None
    asyncio.run(bridge.handle_vk(IncomingVkMessage(message_id=2, peer_id=42, text="ответ")))
    assert bot.reply_targets[-1] == origin.telegram_message_id
    assert "В ответ" not in bot.sent[-1][1]


def test_telegram_reply_points_at_the_saved_vk_message(tmp_path) -> None:
    bridge, store, vk, _groups, _bot = _bridge(tmp_path)
    vk.messages[1] = LoadedMessage(text="исходное", conversation_message_id=15)
    asyncio.run(bridge.handle_vk(IncomingVkMessage(message_id=1, peer_id=42, text="исходное")))
    origin = store.link_by_vk(peer_id=42, conversation_message_id=15)
    assert origin is not None
    asyncio.run(
        bridge.handle_reply(
            chat_id=-100500,
            text="ответ",
            telegram_message_id=900,
            reply_to_message_id=origin.telegram_message_id,
        )
    )
    assert vk.reply_targets == [15]
    saved = store.link_by_telegram(chat_id=-100500, message_id=900)
    assert saved is not None
    assert saved.vk_conversation_message_id == 2001
    assert saved.vk_message_id == 1001


def test_successful_vk_delivery_marks_the_telegram_message(tmp_path) -> None:
    bridge, store, _vk, groups, bot = _bridge(tmp_path)
    store.save_peer(vk_peer_id=42, title="[VK] Анна", telegram_chat_id=-100500, bot_invited=True)
    asyncio.run(bridge.handle_reply(chat_id=-100500, text="привет", telegram_message_id=12))
    asyncio.run(bridge.handle_reply(chat_id=-100999, text="чужой", telegram_message_id=13))
    assert groups.reactions_allowed == [-100500]
    assert groups.reactions == [(-100500, 12, DELIVERED_REACTION)]


def test_reaction_failure_keeps_the_delivered_message(tmp_path) -> None:
    bridge, store, _vk, groups, _bot = _bridge(tmp_path)
    store.save_peer(vk_peer_id=42, title="[VK] Анна", telegram_chat_id=-100500, bot_invited=True)
    groups.fail_react = True
    asyncio.run(bridge.handle_reply(chat_id=-100500, text="привет", telegram_message_id=12))
    saved = store.link_by_telegram(chat_id=-100500, message_id=12)
    assert saved is not None
    assert groups.reaction_attempts == [(-100500, 12)]
    assert groups.reactions == []


def test_read_receipt_sets_eyes_on_outgoing_messages(tmp_path) -> None:
    bridge, store, _vk, groups, _bot = _bridge(tmp_path)
    store.save_message_link(
        telegram_chat_id=-100500,
        telegram_message_id=10,
        vk_peer_id=42,
        vk_message_id=100,
        vk_conversation_message_id=4,
        outgoing=True,
    )
    store.save_message_link(
        telegram_chat_id=-100500,
        telegram_message_id=11,
        vk_peer_id=42,
        vk_message_id=101,
        vk_conversation_message_id=8,
        outgoing=True,
    )
    store.save_message_link(
        telegram_chat_id=-100500,
        telegram_message_id=12,
        vk_peer_id=42,
        vk_message_id=102,
        vk_conversation_message_id=3,
    )
    asyncio.run(bridge.mark_read(42, 9))
    asyncio.run(bridge.mark_read(42, 9))
    assert groups.reactions == [
        (-100500, 10, READ_REACTION),
        (-100500, 11, READ_REACTION),
    ]


def test_read_receipts_do_not_delay_the_incoming_message(tmp_path) -> None:
    bridge, store, _vk, groups, bot = _bridge(tmp_path)
    store.save_message_link(
        telegram_chat_id=-100500,
        telegram_message_id=10,
        vk_peer_id=42,
        vk_message_id=100,
        vk_conversation_message_id=4,
        outgoing=True,
    )
    store.save_message_link(
        telegram_chat_id=-100500,
        telegram_message_id=11,
        vk_peer_id=42,
        vk_message_id=101,
        vk_conversation_message_id=8,
        outgoing=True,
    )
    bridge.note_read(42, 4)
    bridge.note_read(42, 9)
    asyncio.run(bridge.handle_vk(IncomingVkMessage(message_id=1, peer_id=42, text="привет")))
    assert bot.sent
    assert groups.reactions == []
    asyncio.run(bridge.apply_pending_reads())
    assert groups.reactions == [
        (-100500, 10, READ_REACTION),
        (-100500, 11, READ_REACTION),
    ]


def test_vk_edit_keeps_the_previous_text(tmp_path) -> None:
    bridge, store, vk, _groups, bot = _bridge(tmp_path)
    store.save_message_link(
        telegram_chat_id=-100500,
        telegram_message_id=10,
        vk_peer_id=42,
        vk_message_id=50,
        vk_conversation_message_id=4,
        body="Пётр:\nпривет",
    )
    vk.messages[50] = LoadedMessage(text="пока")
    edited = datetime(2026, 10, 15, 20, 41)
    asyncio.run(bridge.handle_vk_edit(peer_id=42, message_id=50, at=edited))
    asyncio.run(bridge.handle_vk_edit(peer_id=42, message_id=50, at=edited))
    assert bot.revised == [(-100500, 10, "Пётр:\nпривет\n\nревизия · 15 окт 20:41\nпока")]


def test_vk_delete_marks_the_telegram_message(tmp_path) -> None:
    bridge, store, _vk, _groups, bot = _bridge(tmp_path)
    store.save_message_link(
        telegram_chat_id=-100500,
        telegram_message_id=10,
        vk_peer_id=42,
        vk_message_id=50,
        vk_conversation_message_id=4,
        body="привет",
    )
    deleted = datetime(2026, 10, 15, 20, 45)
    asyncio.run(bridge.handle_vk_delete(peer_id=42, message_id=50, at=deleted))
    asyncio.run(bridge.handle_vk_delete(peer_id=42, message_id=50, at=deleted))
    assert bot.revised == [(-100500, 10, "привет\n\nудалено · 15 окт 20:45")]


def test_telegram_edit_and_delete_change_the_vk_message(tmp_path) -> None:
    bridge, store, vk, _groups, _bot = _bridge(tmp_path)
    store.save_message_link(
        telegram_chat_id=-100500,
        telegram_message_id=10,
        vk_peer_id=42,
        vk_message_id=100,
        vk_conversation_message_id=4,
        outgoing=True,
    )
    asyncio.run(bridge.handle_edit(chat_id=-100500, telegram_message_id=10, text="новый"))
    asyncio.run(bridge.handle_delete(chat_id=-100500, telegram_message_id=10))
    asyncio.run(bridge.handle_edit(chat_id=-100500, telegram_message_id=99, text="чужое"))
    assert vk.edits == [(42, 100, 4, "новый")]
    assert vk.deletes == [(42, 4)]


def test_own_vk_edit_does_not_rewrite_telegram(tmp_path) -> None:
    bridge, store, vk, _groups, bot = _bridge(tmp_path)
    store.save_message_link(
        telegram_chat_id=-100500,
        telegram_message_id=10,
        vk_peer_id=42,
        vk_message_id=50,
        vk_conversation_message_id=4,
        outgoing=True,
        body="привет",
    )
    vk.messages[50] = LoadedMessage(text="новый")
    asyncio.run(bridge.handle_vk_edit(peer_id=42, message_id=50))
    assert bot.revised == []


def test_presence_updates_the_chat_description(tmp_path) -> None:
    bridge, store, vk, groups, _bot = _bridge(tmp_path)
    store.save_peer(vk_peer_id=42, title="[VK] Анна", telegram_chat_id=-100500, bot_invited=True)
    store.save_peer(vk_peer_id=2_000_000_001, title="[VK] [Группа] Друзья", telegram_chat_id=-1009, bot_invited=True)
    store.save_peer(vk_peer_id=-5, title="[VK] Клуб", telegram_chat_id=-1008, bot_invited=True)
    seen = datetime(2026, 10, 15, 21, 10)
    asyncio.run(bridge.handle_presence(42, online=True, at=seen))
    asyncio.run(bridge.handle_presence(42, online=True, at=seen))
    asyncio.run(bridge.handle_presence(42, online=False, at=seen))
    asyncio.run(bridge.handle_presence(99, online=True, at=seen))
    asyncio.run(bridge.handle_presence(2_000_000_001, online=True, at=seen))
    assert groups.abouts == [(-100500, "онлайн"), (-100500, "был 15 окт 21:10")]
    assert groups.created == []
    vk.presence[42] = (True, None)
    asyncio.run(bridge.refresh_presence())
    assert groups.abouts[-1] == (-100500, "онлайн")


def test_service_chat_shows_status_and_answers_the_command(tmp_path) -> None:
    bridge, _store, vk, groups, bot = _bridge(tmp_path)
    vk.expires_at = datetime(2026, 10, 16, 17, 58).timestamp()
    moment = datetime(2026, 10, 15, 21, 30)
    asyncio.run(bridge.pulse(now=moment))
    assert groups.created == []
    assert groups.service_opened == 1
    assert groups.pins == [(-100777, 501)]
    assert bot.texts[0][0] == -100777
    assert "long poll запускается" in bot.texts[0][1]
    assert "токен VK до 16 окт 17:58" in bot.texts[0][1]
    heartbeat = json.loads((tmp_path / "heartbeat.json").read_text())
    assert heartbeat["chat_id"] == -100777
    bridge.note_poll()
    asyncio.run(bridge.pulse(now=datetime(2026, 10, 15, 21, 33)))
    assert groups.service_opened == 1
    assert "long poll на связи" in bot.revised[-1][2]
    asyncio.run(bridge.handle_reply(chat_id=-100777, text="/status"))
    assert bot.texts[-1][0] == -100777
    assert "мост жив" in bot.texts[-1][1]
    asyncio.run(bridge.handle_reply(chat_id=-100500, text="/status"))
    assert vk.sent == []
    assert all(chat_id == -100777 for chat_id, _text in bot.texts)


def test_add_command_opens_pages_before_the_first_message(tmp_path) -> None:
    bridge, _store, vk, groups, bot = _bridge(tmp_path)
    vk.screens["durov"] = 42
    command = "/a https://vk.com/id42 https://vk.com/durov https://vk.com/nope"
    asyncio.run(bridge.handle_reply(chat_id=77, text=command))
    asyncio.run(bridge.handle_reply(chat_id=-100500, text="/a https://vk.com/id42"))
    assert groups.created == [chat_title("Анна Иванова", 42)]
    assert "создан: [VK] Анна Иванова" in bot.texts[0][1]
    assert "уже есть: [VK] Анна Иванова" in bot.texts[0][1]
    assert "не нашёл: https://vk.com/nope" in bot.texts[0][1]
    assert "уже есть: [VK] Анна Иванова" in bot.texts[1][1]
    assert vk.sent == []


def test_missing_service_chat_is_created_for_a_new_account(tmp_path) -> None:
    bridge, store, _vk, groups, bot = _bridge(tmp_path)
    store.save_runtime(SERVICE_CHAT_KEY, "-1001")
    store.save_runtime(SERVICE_MESSAGE_KEY, "9")
    groups.missing_chats.add(-1001)
    asyncio.run(bridge.pulse(now=datetime(2026, 10, 15, 21, 30)))
    assert groups.service_opened == 1
    assert store.read_runtime_int(SERVICE_CHAT_KEY) == -100777
    assert store.read_runtime_int(SERVICE_MESSAGE_KEY) == 501
    assert bot.texts[0][0] == -100777
    assert bot.revised == []
    assert groups.pins == [(-100777, 501)]


def test_typing_shows_in_an_existing_chat_only(tmp_path) -> None:
    bridge, store, _vk, groups, bot = _bridge(tmp_path)
    store.save_peer(vk_peer_id=42, title="[VK] Анна", telegram_chat_id=-100500, bot_invited=True)
    asyncio.run(bridge.handle_typing(42, now=10))
    asyncio.run(bridge.handle_typing(42, now=10.2))
    asyncio.run(bridge.handle_typing(42, now=12))
    asyncio.run(bridge.handle_typing(99, now=12))
    assert bot.typing == [-100500, -100500]
    assert groups.created == []


def test_unknown_reply_is_sent_without_a_link(tmp_path) -> None:
    bridge, _store, vk, _groups, bot = _bridge(tmp_path)
    vk.messages[1] = LoadedMessage(text="новое", conversation_message_id=15, reply_conversation_message_id=99)
    asyncio.run(bridge.handle_vk(IncomingVkMessage(message_id=1, peer_id=42, text="новое")))
    assert bot.reply_targets == [None]
    asyncio.run(bridge.handle_reply(chat_id=-100500, text="мимо", reply_to_message_id=404))
    assert vk.reply_targets == [None]


def test_broken_reply_link_does_not_drop_the_message(tmp_path) -> None:
    bridge, store, vk, _groups, _bot = _bridge(tmp_path)
    store.save_peer(vk_peer_id=42, title="[VK] Анна", telegram_chat_id=-100500, bot_invited=True)
    store.save_message_link(
        telegram_chat_id=-100500,
        telegram_message_id=10,
        vk_peer_id=42,
        vk_message_id=100,
        vk_conversation_message_id=7,
    )
    vk.fail_reply = True
    asyncio.run(
        bridge.handle_reply(chat_id=-100500, text="обратно", reply_to_message_id=10, telegram_message_id=11)
    )
    assert vk.sent[-1][1] == "обратно"
    assert vk.reply_targets[-1] is None


def test_group_limit_is_posted_once_to_the_service_chat(tmp_path) -> None:
    bridge, store, _vk, groups, bot = _bridge(tmp_path)
    store.save_runtime(SERVICE_CHAT_KEY, "-1009")
    groups.flood_seconds = 7200
    message = IncomingVkMessage(message_id=5, peer_id=42, text="привет")
    asyncio.run(_deliver(bridge=bridge, message=message))
    asyncio.run(_deliver(bridge=bridge, message=message))
    assert groups.created == []
    assert len(bot.texts) == 1
    assert bot.texts[0][0] == -1009
    assert "ошибка · создание чата" in bot.texts[0][1]
    assert "50 новых групп" in bot.texts[0][1]
    assert "peer 42, сообщение 5" in bot.texts[0][1]
    assert "привет" in bot.texts[0][1]
    assert "само не повторится" in bot.texts[0][1]


def test_failed_vk_send_is_posted_to_the_service_chat(tmp_path) -> None:
    bridge, store, vk, _groups, bot = _bridge(tmp_path)
    store.save_runtime(SERVICE_CHAT_KEY, "-1009")
    store.save_peer(vk_peer_id=42, title="[VK] Анна", telegram_chat_id=-100500, bot_invited=True)
    vk.fail_send = True
    asyncio.run(bridge.handle_reply(chat_id=-100500, text="привет"))
    assert vk.sent == []
    assert bot.texts[0][0] == -1009
    assert "отправка в VK" in bot.texts[0][1]
    assert "привет" in bot.texts[0][1]
    assert "не принято" in bot.texts[0][1]


def test_unreachable_service_chat_does_not_kill_the_report(tmp_path) -> None:
    bridge, store, _vk, _groups, bot = _bridge(tmp_path)
    store.save_runtime(SERVICE_CHAT_KEY, "-1009")
    calls = 0

    async def fail_send(*, chat_id: int, text: str) -> int:
        nonlocal calls
        calls += 1
        raise aiohttp.ClientError("Cannot connect to host api.telegram.org:443")

    bot.send_text = fail_send
    moment = datetime(2026, 10, 23, 4, 17)
    asyncio.run(bridge.report(key="pulse", title="статус моста", detail="обрыв", now=moment))
    asyncio.run(bridge.report(key="pulse", title="статус моста", detail="обрыв", now=moment))
    assert calls == 1
