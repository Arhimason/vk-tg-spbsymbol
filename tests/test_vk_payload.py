from vk_tg_bridge.models import LongPollTick
from vk_tg_bridge.vk_payload import (
    LongPollStep,
    conversation_message_id,
    largest_photo_url,
    long_poll_step,
    message_body,
    attachment_tokens,
    parse_message_deleted,
    parse_message_edit,
    add_command_targets,
    page_target,
    parse_new_message,
    parse_presence,
    presence_from_user,
    parse_typing,
    parse_outgoing_read,
    reply_conversation_id,
    vk_reply_forward,
    saved_photo_attachment,
    saved_video_attachment,
    saved_video_message_attachment,
    saved_voice_attachment,
    title_from_chat,
    title_from_groups,
    title_from_users,
    avatar_url_from_chat,
    avatar_url_from_group,
    avatar_url_from_user,
)


def test_incoming_message_is_parsed() -> None:
    update = [4, 1619489, 561, 123456, 1464958914, "hello", {}, {}]
    message = parse_new_message(update)
    assert message is not None
    assert message.message_id == 1619489
    assert message.peer_id == 123456
    assert message.text == "hello"


def test_outgoing_read_is_a_watermark() -> None:
    assert parse_outgoing_read([7, 42, 15]) == (42, 15)
    assert parse_outgoing_read([6, 42, 15]) is None
    assert parse_outgoing_read([4, 1, 0, 42]) is None


def test_edit_and_delete_events() -> None:
    assert parse_message_edit([5, 10, 0, 42, 1_760_546_460, "новый"]) == (10, 42, "новый", 1_760_546_460)
    assert parse_message_edit([5, 10, 0, 42, 0, "новый"]) == (10, 42, "новый", None)
    assert parse_message_deleted([2, 10, 128, 42]) == (10, 42)
    assert parse_message_deleted([2, 10, 1, 42]) is None
    assert parse_message_edit([4, 10, 0, 42]) is None
    tokens = attachment_tokens(
        {"attachments": [{"type": "photo", "photo": {"owner_id": 1, "id": 2, "access_key": "k"}}]}
    )
    assert tokens == "photo1_2_k"


def test_friend_presence_events() -> None:
    assert parse_presence([8, -42, 0, 1_760_546_460]) == (42, True, 1_760_546_460)
    assert parse_presence([9, -42, 1, 1_760_546_460]) == (42, False, 1_760_546_460)
    assert parse_presence([4, 1, 0, 42]) is None
    assert presence_from_user([{"id": 42, "online": 1}]) == (True, None)
    assert presence_from_user([{"id": 42, "last_seen": {"time": 1_760_546_460}}]) == (False, 1_760_546_460)
    assert presence_from_user([{"id": 42}]) == (False, None)


def test_add_command_reads_several_vk_pages() -> None:
    targets = add_command_targets("/a https://vk.com/id42 https://m.vk.ru/club5 vk.com/durov")
    assert targets == ["https://vk.com/id42", "https://m.vk.ru/club5", "vk.com/durov"]
    assert add_command_targets("/a@bridge https://vk.com/id7") == ["https://vk.com/id7"]
    assert add_command_targets("/a") == []
    assert add_command_targets("привет") is None
    assert page_target("https://vk.com/id42") == 42
    assert page_target("https://vk.com/club5") == -5
    assert page_target("https://vk.com/public8") == -8
    assert page_target("https://vk.com/im?sel=-12") == -12
    assert page_target("https://vk.com/durov") == "durov"
    assert page_target("https://example.com/id1") is None


def test_typing_events_point_at_the_dialog() -> None:
    assert parse_typing([61, 42, 1]) == 42
    assert parse_typing([61, -173, 1]) == -173
    assert parse_typing([62, 7, 464]) == 2_000_000_464
    assert parse_typing([63, 7, 2_000_000_464, 1, 10]) == 2_000_000_464
    assert parse_typing([4, 1, 0, 42]) is None
    assert parse_typing([61]) is None


def test_outbox_and_deleted_messages_are_ignored() -> None:
    assert parse_new_message([4, 1, 2, 10, 0, "out"]) is None
    assert parse_new_message([4, 1, 128, 10, 0, "deleted"]) is None
    assert parse_new_message([61, 1]) is None
    assert parse_new_message("nope") is None


def test_long_poll_failures() -> None:
    assert long_poll_step(LongPollTick(failed=1, ts=9)) is LongPollStep.NEW_TS
    assert long_poll_step(LongPollTick(failed=2)) is LongPollStep.NEW_SERVER
    assert long_poll_step(LongPollTick(failed=3)) is LongPollStep.NEW_SERVER
    assert long_poll_step(LongPollTick(failed=4)) is LongPollStep.BAD_VERSION
    assert long_poll_step(LongPollTick(ts=3, updates=[])) is LongPollStep.EVENTS


def test_photo_and_other_attachments() -> None:
    text, files = message_body(
        {
            "text": "смотри",
            "reply_message": {"text": "цитата"},
            "fwd_messages": [
                {
                    "text": "старое",
                    "attachments": [
                        {"type": "doc", "doc": {"title": "a.pdf", "url": "https://vk.test/a.pdf", "ext": "pdf"}},
                    ],
                }
            ],
            "attachments": [
                {
                    "type": "photo",
                    "photo": {
                        "sizes": [
                            {"width": 100, "url": "https://vk.test/small.jpg"},
                            {"width": 800, "url": "https://vk.test/big.jpg"},
                        ]
                    },
                },
                {"type": "audio_message", "audio_message": {"link_ogg": "https://vk.test/v.ogg"}},
                {
                    "type": "sticker",
                    "sticker": {
                        "images": [
                            {"width": 64, "url": "https://vk.test/s.png"},
                            {"width": 128, "url": "https://vk.test/s-big.png"},
                        ]
                    },
                },
                {"type": "doc"},
                {
                    "type": "video",
                    "video": {
                        "title": "ролик",
                        "files": {
                            "mp4_360": "https://vk.test/360.mp4",
                            "mp4_720": "https://vk.test/720.mp4",
                        },
                    },
                },
                {"type": "poll", "poll": {"question": "Идём?", "answers": [{"text": "да"}]}},
                {"type": "link", "link": {"url": "https://vk.test/l", "title": "Ссылка"}},
                {"type": "gift"},
            ],
        }
    )
    assert [(item.kind, item.url) for item in files] == [
        ("photo", "https://vk.test/big.jpg"),
        ("voice", "https://vk.test/v.ogg"),
        ("photo", "https://vk.test/s-big.png"),
        ("video", "https://vk.test/720.mp4"),
        ("document", "https://vk.test/a.pdf"),
    ]
    assert "смотри" in text
    assert "[файл]" in text
    assert "В ответ" not in text
    assert "Переслано: старое" in text
    assert "Опрос: Идём?" in text
    assert "Ссылка" in text
    assert "[подарок]" in text


def test_reply_stays_a_link_instead_of_a_quote() -> None:
    raw = {
        "text": "ответ",
        "conversation_message_id": 15,
        "reply_message": {
            "text": "цитата",
            "conversation_message_id": 7,
            "attachments": [
                {"type": "photo", "photo": {"sizes": [{"width": 100, "url": "https://vk.test/q.jpg"}]}},
            ],
        },
    }
    text, files = message_body(raw)
    assert text == "ответ"
    assert files == []
    assert conversation_message_id(raw) == 15
    assert reply_conversation_id(raw) == 7
    assert reply_conversation_id({"text": "просто"}) is None
    assert vk_reply_forward(peer_id=42, conversation_message_id=7) == (
        '{"peer_id":42,"conversation_message_ids":[7],"is_reply":true}'
    )


def test_largest_photo_falls_back_to_original() -> None:
    url = largest_photo_url({"orig_photo": {"url": "https://vk.test/orig.jpg"}})
    assert url == "https://vk.test/orig.jpg"


def test_titles() -> None:
    assert title_from_users([{"first_name": "Анна", "last_name": "Иванова"}]) == "Анна Иванова"
    assert title_from_groups({"groups": [{"name": "Стена"}]}) == "Стена"
    assert title_from_chat(
        {"items": [{"chat_settings": {"title": "Беседа"}}]}
    ) == "Беседа"
    assert title_from_users([]) == ""


def test_avatar_url_skips_missing_and_placeholder_photos() -> None:
    user = avatar_url_from_user(
        [{"has_photo": 1, "photo_200": "https://vk.test/a.jpg", "photo_max_orig": "https://vk.test/big.jpg"}]
    )
    empty = avatar_url_from_user([{"has_photo": 0, "photo_200": "https://vk.com/images/camera_200.png"}])
    group = avatar_url_from_group({"groups": [{"photo_200": "https://vk.com/images/community_200.png"}]})
    chat = avatar_url_from_chat(
        {"items": [{"chat_settings": {"photo": {"photo_200": "https://vk.test/chat.jpg"}}}]}
    )
    assert user == "https://vk.test/big.jpg"
    assert empty is None
    assert group is None
    assert chat == "https://vk.test/chat.jpg"


def test_saved_photo_and_voice_become_attachments() -> None:
    photo = saved_photo_attachment([{"owner_id": 10, "id": 20, "access_key": "abc"}])
    voice = saved_voice_attachment(
        {"type": "audio_message", "audio_message": {"owner_id": 10, "id": 30}}
    )
    assert photo == "photo10_20_abc"
    assert voice == "audio_message10_30"
    assert saved_video_attachment(
        {"owner_id": 10, "video_id": 40, "access_key": "key"}
    ) == "video10_40_key"
    assert saved_video_message_attachment({"owner_id": 10, "video_id": 40}) == "video_message10_40"


def test_vk_video_message_stays_a_circle() -> None:
    _text, files = message_body(
        {
            "attachments": [
                {
                    "type": "video_message",
                    "video_message": {"files": {"mp4_480": "https://vk.test/circle.mp4"}},
                }
            ]
        }
    )
    assert [(item.kind, item.filename, item.url) for item in files] == [
        ("video_note", "video_note.mp4", "https://vk.test/circle.mp4")
    ]
