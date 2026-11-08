import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.db.session import Database

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def database_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Run bash scripts/test_integration.sh for disposable PostgreSQL tests")
    if make_url(url).database != "language_bot_test":
        raise RuntimeError(
            "Integration tests require a disposable database named language_bot_test"
        )
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        yield url
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture
async def db(database_url):
    database = Database(database_url)
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE audio_jobs, archive_jobs, audio_assets, stored_media, interaction_states, sentences, model_jobs, operations, access_changes, users RESTART IDENTITY"
            )
        )
    yield database
    await database.close()


@pytest.fixture
def config(database_url):
    return Settings(
        _env_file=None,
        database_url=database_url,
        telegram_bot_token="12345:integration-test",
        telegram_webhook_secret="a" * 32,
        admin_telegram_id=11,
        admin_notification_channel_id=-10099,
    )
