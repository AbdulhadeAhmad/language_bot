import argparse
import asyncio
import logging
import signal

from app.bot.transport import BotTransport
from app.config import Settings
from app.db.session import Database
from app.logging import configure_logging
from app.services.delivery import DeliveryService
from app.services.inbox import InboxService

logger = logging.getLogger(__name__)


async def serve(role: str) -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    database = Database(settings.database_url.get_secret_value())
    transport = (
        BotTransport(settings.telegram_bot_token.get_secret_value())
        if role == "deliveries"
        else None
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    service = (
        DeliveryService(database, settings, transport)
        if transport is not None
        else InboxService(database, settings)
    )
    try:
        if not await database.ready():
            raise RuntimeError("Apply migrations before starting workers")
        logger.info("worker_started", extra={"worker_role": role})
        while not stop.is_set():
            try:
                worked = await service.process_one()
            except Exception as exc:
                logger.error(
                    "worker_operation_failed",
                    extra={"worker_role": role, "error_type": type(exc).__name__},
                )
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
                except TimeoutError:
                    pass
    finally:
        if transport is not None:
            await transport.close()
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["interactions", "deliveries"], required=True)
    asyncio.run(serve(parser.parse_args().role))
