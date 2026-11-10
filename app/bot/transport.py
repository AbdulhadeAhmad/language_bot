import io
import logging
import re
from dataclasses import dataclass
from html import unescape
from typing import Protocol

from telebot.apihelper import ApiTelegramException as SyncApiTelegramException
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException, session_manager
from telebot.types import InlineKeyboardMarkup, InputFile, ReplyParameters

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UploadedAudio:
    message_id: int
    file_id: str


def delivery_error(exc):
    if exc.error_code == 429:
        seconds = exc.result_json.get("parameters", {}).get("retry_after", 1)
        raise RetryAfter(int(seconds)) from None
    if exc.error_code >= 500:
        raise ConnectionError("Telegram server error") from None
    raise RejectedDelivery(exc.description, telegram_error_code=exc.error_code) from None


class RetryAfter(Exception):
    def __init__(self, seconds: int) -> None:
        self.seconds = max(1, seconds)


class RejectedDelivery(Exception):
    """Telegram explicitly rejected the operation."""

    def __init__(self, message="Telegram rejected the delivery", *, telegram_error_code=None):
        super().__init__(message)
        self.telegram_error_code = telegram_error_code


class MissingStatusMessage(RejectedDelivery):
    """The known status message was deleted or can no longer be edited."""


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
            if method == "copy_message":
                result = await self.bot.copy_message(**payload)
                return result.message_id
            if method == "delete_message":
                return int(bool(await self.bot.delete_message(**payload)))
            if method == "edit_message_reply_markup":
                result = await self.bot.edit_message_reply_markup(**payload)
                return getattr(result, "message_id", None)
            if method == "edit_message_caption":
                result = await self.bot.edit_message_caption(**payload)
                return getattr(result, "message_id", None)
            if method == "edit_message_text":
                result = await self.bot.edit_message_text(**payload)
                return getattr(result, "message_id", None)
            raise RejectedDelivery("Unsupported delivery method")
        except (ApiTelegramException, SyncApiTelegramException) as exc:
            if (
                method == "delete_message"
                and exc.error_code == 400
                and "message to delete not found" in exc.description.lower()
            ):
                return 1
            if (
                method in {"edit_message_text", "edit_message_reply_markup", "edit_message_caption"}
                and exc.error_code == 400
                and "message is not modified" in exc.description.lower()
            ):
                return payload.get("message_id")
            if (
                method in {"edit_message_text", "edit_message_reply_markup"}
                and exc.error_code == 400
                and any(
                    value in exc.description.lower()
                    for value in ("message to edit not found", "message can't be edited")
                )
            ):
                raise MissingStatusMessage(
                    exc.description, telegram_error_code=exc.error_code
                ) from None
            delivery_error(exc)

    async def upload_audio(
        self,
        channel_id: int,
        audio: bytes,
        *,
        caption: str,
        title: str,
        performer: str,
        file_name: str,
        parse_mode: str,
    ) -> UploadedAudio:
        try:
            with io.BytesIO(audio) as stream:
                message = await self.bot.send_audio(
                    channel_id,
                    InputFile(stream, file_name=file_name),
                    caption=caption,
                    parse_mode=parse_mode,
                    title=title,
                    performer=performer,
                    disable_notification=True,
                )
            if not message.audio or not message.audio.file_id:
                error = ConnectionError("Audio receipt unavailable")
                error.message_id = message.message_id
                raise error
            logger.info(
                "archive_audio_metadata_confirmed",
                extra={
                    "archive_chat_id": channel_id,
                    "archive_message_id": message.message_id,
                    "caption_present": bool(getattr(message, "caption", None)),
                    "caption_matches": getattr(message, "caption", None)
                    == unescape(re.sub(r"</?b>", "", caption)),
                    "caption_has_hashtags": any(
                        entity.type == "hashtag"
                        for entity in (getattr(message, "caption_entities", None) or [])
                    ),
                    "title_matches": getattr(message.audio, "title", None) == title,
                    "file_name_matches": getattr(message.audio, "file_name", None) == file_name,
                },
            )
            return UploadedAudio(message.message_id, message.audio.file_id)
        except (ApiTelegramException, SyncApiTelegramException) as exc:
            delivery_error(exc)

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
