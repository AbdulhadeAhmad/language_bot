import json
import logging
import socket
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

from app.paths import data_root

FIELDS = (
    "user_id",
    "entry_id",
    "job_id",
    "update_id",
    "delivery_id",
    "worker_role",
    "operation",
    "error_type",
    "effect_key",
    "method",
    "telegram_error_code",
    "telegram_description",
    "error",
    "media_id",
    "archive_chat_id",
    "archive_message_id",
    "caption_present",
    "caption_matches",
    "caption_has_hashtags",
    "title_matches",
    "file_name_matches",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        payload.update({key: getattr(record, key) for key in FIELDS if hasattr(record, key)})
        return json.dumps(payload)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    directory = data_root() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    import os

    file_handler = RotatingFileHandler(
        directory / f"app-{socket.gethostname()}-{os.getpid()}.jsonl",
        maxBytes=5_000_000,
        backupCount=3,
    )
    file_handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler, file_handler], force=True)
