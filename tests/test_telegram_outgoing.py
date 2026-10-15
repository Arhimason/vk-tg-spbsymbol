import asyncio

from vk_tg_bridge.constants import ATTACHMENT_FAILED_NOTE
from vk_tg_bridge.telegram_user import (
    counts_as_own_message,
    outgoing_parts,
    telegram_message_id,
    telegram_reply_target,
)


def test_other_members_count_as_own_when_the_switch_is_on() -> None:
    own = counts_as_own_message(
        relay_members=True,
        outgoing=True,
        sender_id=1,
        bot_id=9,
        service=False,
    )
    friend = counts_as_own_message(
        relay_members=True,
        outgoing=False,
        sender_id=2,
        bot_id=9,
        service=False,
    )
    bot = counts_as_own_message(
        relay_members=True,
        outgoing=False,
        sender_id=9,
        bot_id=9,
        service=False,
    )
    joined = counts_as_own_message(
        relay_members=True,
        outgoing=False,
        sender_id=2,
        bot_id=9,
        service=True,
    )
    assert own
    assert friend
    assert not bot
    assert not joined
    assert not counts_as_own_message(
        relay_members=False,
        outgoing=False,
        sender_id=2,
        bot_id=9,
        service=False,
    )


class _File:
    def __init__(self, *, mime_type: str = "", name: str | None = None) -> None:
        self.mime_type = mime_type
        self.name = name


class _Event:
    def __init__(self, *, text: str = "", payload: bytes = b"", file: _File | None = None, **flags: object) -> None:
        self.raw_text = text
        self.payload = payload
        self.file = file
        self.fwd_from = flags.pop("fwd_from", None)
        self.contact = flags.pop("contact", None)
        self.geo = flags.pop("geo", None)
        self.downloaded = 0
        for name in ("photo", "voice", "sticker", "gif", "video_note", "video", "audio", "document"):
            setattr(self, name, flags.get(name))

    async def download_media(self, *, file: type[bytes]) -> bytes:
        assert file is bytes
        self.downloaded += 1
        return self.payload


class _Fwd:
    from_name = "Маша"
    post_author = None


class _Contact:
    first_name = "Иван"
    last_name = "Петров"
    phone_number = "+7999"


def test_outgoing_photo_voice_and_plain_text() -> None:
    photo = _Event(text="кадр", photo=True, payload=b"jpeg")
    voice = _Event(voice=True, payload=b"ogg")
    text_only = _Event(text="просто текст")
    photo_text, photo_files = asyncio.run(outgoing_parts(photo))
    voice_text, voice_files = asyncio.run(outgoing_parts(voice))
    plain_text, plain_files = asyncio.run(outgoing_parts(text_only))
    assert photo_text == "кадр"
    assert [(item.kind, item.filename, item.content) for item in photo_files] == [("photo", "photo.jpg", b"jpeg")]
    assert voice_text == ""
    assert [(item.kind, item.content) for item in voice_files] == [("voice", b"ogg")]
    assert plain_text == "просто текст"
    assert plain_files == []
    assert text_only.downloaded == 0


def test_forward_sticker_video_note_and_contact() -> None:
    forwarded = _Event(text="привет", fwd_from=_Fwd())
    sticker = _Event(sticker=True, payload=b"webp", file=_File(mime_type="image/webp"))
    video_note = _Event(video_note=True, payload=b"mp4")
    contact = _Event(contact=_Contact())
    empty = _Event(sticker=True, payload=b"")

    forwarded_text, forwarded_files = asyncio.run(outgoing_parts(forwarded))
    _, sticker_files = asyncio.run(outgoing_parts(sticker))
    _, video_files = asyncio.run(outgoing_parts(video_note))
    contact_text, contact_files = asyncio.run(outgoing_parts(contact))
    failed_text, failed_files = asyncio.run(outgoing_parts(empty))

    assert forwarded_text == "Переслано от Маша\nпривет"
    assert forwarded_files == []
    assert sticker_files[0].kind == "document"
    assert sticker_files[0].filename == "sticker.webp"
    assert sticker_files[0].content == b"webp"
    assert video_files[0].kind == "video_note"
    assert video_files[0].filename == "video_note.mp4"
    assert contact_text == "Контакт: Иван Петров +7999"
    assert contact_files == []
    assert ATTACHMENT_FAILED_NOTE in failed_text
    assert failed_files == []


class _ReplyHeader:
    reply_to_msg_id = 77


class _Outgoing:
    def __init__(self) -> None:
        self.message = type("Message", (), {"id": 12})()
        self.reply_to = _ReplyHeader()


def test_outgoing_reply_ids() -> None:
    event = _Outgoing()
    assert telegram_message_id(event) == 12
    assert telegram_reply_target(event) == 77
    assert telegram_reply_target(_Event()) is None
