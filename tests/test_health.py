from datetime import datetime

from vk_tg_bridge.health import (
    decide_watch,
    format_status,
    is_status_command,
    silence_notice,
)


WHEN = datetime(2026, 10, 15, 21, 30)
TOKEN = datetime(2026, 10, 16, 17, 58)


def test_status_text_reports_poll_and_token() -> None:
    starting = format_status(now=WHEN, poll_state="starting", token_until=TOKEN)
    assert starting == "мост жив\n15 окт 21:30\nlong poll запускается\nтокен VK до 16 окт 17:58"
    alive = format_status(now=WHEN, poll_state="ok", token_until=None)
    assert "long poll на связи" in alive
    assert "срок токена VK неизвестен" in alive
    stalled = format_status(now=WHEN, poll_state="stale", token_until=TOKEN)
    assert "long poll не отвечает" in stalled


def test_status_command_is_only_the_bare_word() -> None:
    assert is_status_command("/status")
    assert is_status_command("  Статус ")
    assert is_status_command("/status@bridge")
    assert not is_status_command("статус пожалуйста")
    assert not is_status_command("привет")


def test_watch_alerts_once_until_the_pulse_returns() -> None:
    assert silence_notice(WHEN) == "мост молчит с 15 окт 21:30"
    fresh = decide_watch(heartbeat_at=100, now=200, stale_after=600, alerted=False, chat_id=-100)
    assert fresh.action == "wait"
    stale = decide_watch(heartbeat_at=100, now=800, stale_after=600, alerted=False, chat_id=-100)
    assert stale.action == "alert"
    assert stale.notice == silence_notice(datetime.fromtimestamp(100))
    again = decide_watch(heartbeat_at=100, now=900, stale_after=600, alerted=True, chat_id=-100)
    assert again.action == "wait"
    recovered = decide_watch(heartbeat_at=1000, now=1100, stale_after=600, alerted=True, chat_id=-100)
    assert recovered.action == "clear"
    missing = decide_watch(heartbeat_at=None, now=800, stale_after=600, alerted=False, chat_id=None)
    assert missing.action == "wait"
