"""External runtime paths shared by directly executable benchmark scripts."""
import os
from pathlib import Path

DATA_ROOT = Path(os.environ.get("LANGUAGE_BOT_DATA_DIR", "~/language_bot_data")).expanduser()
if not DATA_ROOT.is_absolute():
    raise ValueError("LANGUAGE_BOT_DATA_DIR must be absolute")
ARTIFACTS = DATA_ROOT / "benchmarks"
MODELS = DATA_ROOT / "models"
