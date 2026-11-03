"""Explicit operational command; startup never changes Telegram configuration."""

import argparse
import asyncio
from urllib.parse import urlsplit

from telebot.async_telebot import AsyncTeleBot

from app.config import Settings


async def register(url: str) -> None:
    if urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
        raise ValueError("Webhook URL must be public HTTPS")
    settings = Settings()
    bot = AsyncTeleBot(settings.telegram_bot_token.get_secret_value())
    try:
        success = await bot.set_webhook(
            url=url,
            secret_token=settings.telegram_webhook_secret.get_secret_value(),
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
        )
        if not success:
            raise RuntimeError("Telegram did not register the webhook")
        print("Webhook registered.")
    finally:
        await bot.close_session()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    asyncio.run(register(parser.parse_args().url))
