import asyncio

from vk_tg_bridge.telegram_bot import TelegramBot, TelegramBotError


def test_rejected_reply_is_sent_again_without_the_link() -> None:
    bot = TelegramBot(session=None, token="token")
    calls: list[dict[str, object]] = []

    async def _call(*, method: str, body: object) -> dict[str, int]:
        assert isinstance(body, dict)
        calls.append(body)
        if body.get("reply_to_message_id") is not None:
            raise TelegramBotError("reply")
        return {"message_id": 5}

    bot._call = _call
    sent = asyncio.run(bot.deliver(chat_id=1, text="ответ", files=[], reply_to_message_id=9))
    assert sent == [5]
    assert calls[0]["reply_to_message_id"] == 9
    assert "reply_to_message_id" not in calls[1]
