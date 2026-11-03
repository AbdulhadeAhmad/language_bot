import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import Base

target_metadata = Base.metadata


def get_url() -> str:
    # Alembic is DB-only; bot credentials are unnecessary for migrations.
    from dotenv import load_dotenv

    from app.paths import data_root

    load_dotenv(data_root() / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url or not url.startswith("postgresql+asyncpg://"):
        raise ValueError("Set DATABASE_URL to a postgresql+asyncpg URL")
    return url


def run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(get_url(), poolclass=pool.NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=get_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
