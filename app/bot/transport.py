from typing import Protocol

from telebot.apihelper import ApiTelegramException as SyncApiTelegramException
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException, session_manager
from telebot.types import InlineKeyboardMarkup


class RetryAfter(Exception):
    def __init__(self, seconds: int) -> None:
        self.seconds = max(1, seconds)


class RejectedDelivery(Exception):
    """Telegram explicitly rejected the operation."""


class TelegramTransport(Protocol):
    async def send(self, method: str, payload: dict) -> int | None: ...
    async def close(self) -> None: ...


class BotTransport:
    def __init__(self, token: str) -> None:
        self.bot = AsyncTeleBot(token)

    async def send(self, method: str, payload: dict) -> int | None:
        payload = dict(payload)
        if isinstance(payload.get("reply_markup"), dict):
            payload["reply_markup"] = InlineKeyboardMarkup.de_json(payload["reply_markup"])
        try:
            if method == "send_message":
                result = await self.bot.send_message(**payload)
                return result.message_id
            if method == "answer_callback_query":
                await self.bot.answer_callback_query(**payload)
                return None
            if method == "edit_message_text":
                result = await self.bot.edit_message_text(**payload)
                return getattr(result, "message_id", None)
            raise RejectedDelivery("Unsupported delivery method")
        except (ApiTelegramException, SyncApiTelegramException) as exc:
            if exc.error_code == 429:
                seconds = exc.result_json.get("parameters", {}).get("retry_after", 1)
                raise RetryAfter(int(seconds)) from None
            # A server error can have an uncertain outcome; do not automatically resend.
            if exc.error_code >= 500:
                raise ConnectionError("Telegram server error") from None
            raise RejectedDelivery("Telegram rejected the delivery") from None

    async def close(self) -> None:
        if session_manager.session is not None:
            await self.bot.close_session()
