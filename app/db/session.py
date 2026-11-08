from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

SCHEMA_REVISION = "0011_review_state"


class Database:
    def __init__(self, url: str, *, engine: AsyncEngine | None = None) -> None:
        self.engine = engine or create_async_engine(
            url, pool_pre_ping=True, hide_parameters=True, connect_args={"timeout": 3}
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def ready(self) -> bool:
        async with self.engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            return revision == SCHEMA_REVISION

    async def close(self) -> None:
        await self.engine.dispose()
