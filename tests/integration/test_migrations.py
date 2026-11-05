from pathlib import Path

from alembic import command
from alembic.config import Config


def test_schema_matches_models(database_url):
    command.check(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")))


def test_upgrade_preserves_users_and_channel_reference_but_removes_updates(
    database_url, monkeypatch
):
    import asyncio

    import asyncpg
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    assert url.database == "language_bot_test"
    migration_url = url.set(database="language_bot_migration_test")

    def native(value):
        return value.set(drivername="postgresql").render_as_string(hide_password=False)

    async def create():
        connection = await asyncpg.connect(native(url))
        try:
            await connection.execute("CREATE DATABASE language_bot_migration_test")
        finally:
            await connection.close()

    async def seed():
        connection = await asyncpg.connect(native(migration_url))
        try:
            await connection.execute("INSERT INTO users (id,status) VALUES (22,'approved')")
            await connection.execute(
                "INSERT INTO incoming_updates (update_id,actor_id,chat_id,payload) VALUES (1,22,22,'{}')"
            )
            await connection.execute(
                "INSERT INTO telegram_deliveries (update_id,actor_id,method,payload,action_key,state,telegram_message_id) VALUES (1,22,'send_message','{}','request','sent',9001)"
            )
        finally:
            await connection.close()

    async def check():
        connection = await asyncpg.connect(native(migration_url))
        try:
            row = await connection.fetchrow(
                "SELECT status,approval_message_id FROM users WHERE id=22"
            )
            assert row["status"] == "approved" and row["approval_message_id"] == 9001
            assert await connection.fetchval("SELECT to_regclass('incoming_updates')") is None
            assert await connection.fetchval("SELECT to_regclass('telegram_deliveries')") is None
            sentence = await connection.fetchrow("SELECT * FROM sentences WHERE user_id=22")
            assert sentence["german"] == "  Gru\u0308ße\t aus Berlin. "
            assert sentence["normalized_german"] == "Grüße aus Berlin."
            assert sentence["english"] == "Greetings from Berlin."
            assert await connection.fetchval("SELECT private FROM users WHERE id=22")
            assert (
                await connection.fetchval(
                    "SELECT m.telegram_file_id FROM audio_assets a JOIN stored_media m ON m.id=a.stored_media_id WHERE a.sentence_id=$1",
                    sentence["id"],
                )
                == "original-recording"
            )
            job = await connection.fetchrow(
                "SELECT * FROM archive_jobs WHERE media_id=$1", sentence["id"]
            )
            assert job["state"] == "pending" and job["attempts"] == 0
            assert job["source_chat_id"] == 22 and job["source_message_id"] == 1
        finally:
            await connection.close()

    async def drop():
        connection = await asyncpg.connect(native(url))
        try:
            await connection.execute("DROP DATABASE language_bot_migration_test WITH (FORCE)")
        finally:
            await connection.close()

    asyncio.run(create())
    try:
        monkeypatch.setenv("DATABASE_URL", migration_url.render_as_string(hide_password=False))
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        command.upgrade(config, "0002_access")
        asyncio.run(seed())
        command.upgrade(config, "0003_operations")

        async def seed_capture():
            connection = await asyncpg.connect(native(migration_url))
            try:
                await connection.execute(
                    "INSERT INTO operations (id,user_id,chat_id,source_message_id,german,english,source_file_id,step,state) "
                    "VALUES ('00000000-0000-0000-0000-000000000001',22,22,1,$1,'Greetings from Berlin.','original-recording','complete','done')",
                    "  Gru\u0308ße\t aus Berlin. ",
                )
            finally:
                await connection.close()

        asyncio.run(seed_capture())
        command.upgrade(config, "head")
        asyncio.run(check())
        command.downgrade(config, "0003_operations")
        command.upgrade(config, "head")
        asyncio.run(check())
    finally:
        asyncio.run(drop())
