from vk_tg_bridge.models import VkCredentials
from vk_tg_bridge.store import Store


def test_peer_roundtrip_and_seen_messages(tmp_path) -> None:
    store = Store(str(tmp_path / "bridge.db"))
    peer = store.save_peer(
        vk_peer_id=42,
        title="Анна",
        telegram_chat_id=-100777,
        bot_invited=False,
    )
    assert store.peer_by_vk(42) == peer
    assert store.peer_by_telegram(-100777) == peer
    store.mark_bot_invited(42)
    assert store.peer_by_vk(42).bot_invited is True
    assert store.is_seen(7) is False
    store.mark_seen(7)
    assert store.is_seen(7) is True
    store.save_cursor(99)
    assert store.read_cursor() == 99
    store.close()


def test_message_link_roundtrip(tmp_path) -> None:
    store = Store(str(tmp_path / "bridge.db"))
    store.save_message_link(
        telegram_chat_id=-100500,
        telegram_message_id=10,
        vk_peer_id=42,
        vk_message_id=100,
        vk_conversation_message_id=7,
    )
    store.save_message_link(
        telegram_chat_id=-100500,
        telegram_message_id=11,
        vk_peer_id=42,
        vk_message_id=100,
        vk_conversation_message_id=7,
    )
    by_telegram = store.link_by_telegram(chat_id=-100500, message_id=11)
    by_vk = store.link_by_vk(peer_id=42, conversation_message_id=7)
    assert by_telegram is not None
    assert by_telegram.vk_conversation_message_id == 7
    assert by_telegram.vk_message_id == 100
    assert by_vk is not None
    assert by_vk.telegram_message_id == 10
    assert store.link_by_telegram(chat_id=-100500, message_id=99) is None
    store.save_message_link(
        telegram_chat_id=-100500,
        telegram_message_id=12,
        vk_peer_id=42,
        vk_message_id=101,
        vk_conversation_message_id=4,
        outgoing=True,
    )
    store.save_message_link(
        telegram_chat_id=-100500,
        telegram_message_id=13,
        vk_peer_id=42,
        vk_message_id=102,
        vk_conversation_message_id=9,
        outgoing=True,
    )
    unread = store.outgoing_unread(peer_id=42, up_to=5)
    assert [link.telegram_message_id for link in unread] == [12]
    store.mark_link_read(chat_id=-100500, message_id=12)
    still_open = store.outgoing_unread(peer_id=42, up_to=9)
    assert [link.telegram_message_id for link in still_open] == [13]
    store.close()


def test_rotated_credentials_roundtrip(tmp_path) -> None:
    store = Store(str(tmp_path / "bridge.db"))
    credentials = VkCredentials(
        access_token="access",
        refresh_token="refresh",
        exchange_token="exchange",
        device_id="device",
        client_id="1",
        expires_at=10.5,
    )
    store.save_credentials(credentials)
    assert store.read_credentials() == credentials
    store.close()
