import os
from pathlib import Path


def data_root() -> Path:
    root = Path(os.environ.get("LANGUAGE_BOT_DATA_DIR", "~/language_bot_data")).expanduser()
    if not root.is_absolute():
        raise ValueError("LANGUAGE_BOT_DATA_DIR must be absolute")
    return root
