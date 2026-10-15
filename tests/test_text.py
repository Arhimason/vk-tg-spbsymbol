from datetime import datetime

from vk_tg_bridge.text import append_version, chat_title, clip_message, mark_deleted, presence_about

EDITED = datetime(2026, 10, 15, 20, 41)
LATER = datetime(2026, 10, 15, 20, 50)


def test_chat_title_is_compact_and_limited() -> None:
    assert chat_title("  Анна\nИванова ", 5) == "[VK] Анна Иванова"
    assert chat_title("", 5) == "[VK] 5"
    assert chat_title("[VK] Анна", 5) == "[VK] Анна"
    assert chat_title("Друзья", 5, group=True) == "[VK] [Группа] Друзья"
    assert len(chat_title("я" * 200, 1)) == 128


def test_versions_accumulate_and_deletion_is_marked() -> None:
    first = append_version("Пётр:\nпривет", "пока", EDITED)
    assert first == "Пётр:\nпривет\n\nревизия · 15 окт 20:41\nпока"
    assert append_version(first, "пока", LATER) == first
    assert append_version("Пётр:\nпривет", "привет", EDITED) == "Пётр:\nпривет"
    second = append_version(first, "до завтра", LATER)
    assert second == "Пётр:\nпривет\n\nревизия · 15 окт 20:41\nпока\n\nревизия · 15 окт 20:50\nдо завтра"
    assert mark_deleted("привет", datetime(2026, 10, 15, 20, 45)) == "привет\n\nудалено · 15 окт 20:45"
    assert mark_deleted("привет\n\nудалено · 15 окт 20:45", LATER) == "привет\n\nудалено · 15 окт 20:45"
    assert mark_deleted("", EDITED) == "удалено · 15 окт 20:41"


def test_presence_about_names_online_and_last_seen() -> None:
    assert presence_about(online=True, moment=None) == "онлайн"
    assert presence_about(online=False, moment=datetime(2026, 10, 15, 21, 10)) == "был 15 окт 21:10"
    assert presence_about(online=False, moment=None) == "не в сети"


def test_clip_message() -> None:
    assert clip_message("  привет ") == "привет"
    assert clip_message("   ") == "[сообщение без текста]"
    assert len(clip_message("я" * 5000)) == 4096
