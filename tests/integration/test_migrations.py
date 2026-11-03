from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]


def test_schema_matches_models_and_migrations_round_trip(database_url):
    config = Config(str(ROOT / "alembic.ini"))
    command.check(config)
    # Preserve the explicit downgrade safety check for populated multi-action outboxes.
    import asyncio

    from sqlalchemy import text

    from app.db.session import Database

    async def clear():
        db = Database(database_url)
        async with db.engine.begin() as conn:
            await conn.execute(text("TRUNCATE telegram_deliveries"))
        await db.close()

    asyncio.run(clear())
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.check(config)
