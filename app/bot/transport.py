from typing import Protocol

from telebot.apihelper import ApiTelegramException as SyncApiTelegramException
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException, session_manager
from telebot.types import InlineKeyboardMarkup, ReplyParameters


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
        if isinstance(payload.get("reply_parameters"), dict):
            payload["reply_parameters"] = ReplyParameters.de_json(payload["reply_parameters"])
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
            if (
                method == "edit_message_text"
                and exc.error_code == 400
                and "message is not modified" in exc.description.lower()
            ):
                return payload.get("message_id")
            if exc.error_code == 429:
                seconds = exc.result_json.get("parameters", {}).get("retry_after", 1)
                raise RetryAfter(int(seconds)) from None
            # A server error can have an uncertain outcome; do not automatically resend.
            if exc.error_code >= 500:
                raise ConnectionError("Telegram server error") from None
            raise RejectedDelivery("Telegram rejected the delivery") from None

    async def download(self, file_id: str) -> bytes:
        import aiohttp

        info = await self.bot.get_file(file_id)
        if info.file_size and info.file_size > 20 * 1024 * 1024:
            raise ValueError("AudioTooLarge")
        url = f"https://api.telegram.org/file/bot{self.bot.token}/{info.file_path}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as client:
            async with client.get(url) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > 20 * 1024 * 1024:
                        raise ValueError("AudioTooLarge")
                return bytes(data)

    async def close(self) -> None:
        if session_manager.session is not None:
            await self.bot.close_session()
