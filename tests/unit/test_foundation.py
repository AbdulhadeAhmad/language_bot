import asyncio
import json
import logging
import wave
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.bot.updates import actor, parse_update
from app.config import Settings
from app.logging import JsonFormatter
from app.main import create_app
from app.providers.base import VoiceConfig
from app.providers.fake import fake_providers


def settings(**overrides):
    return Settings(
        _env_file=None,
        **{
            "database_url": "postgresql+asyncpg://postgres:test@localhost/language_bot_test",
            "telegram_bot_token": "12345:unit-test-token",
            "telegram_webhook_secret": "a" * 32,
            "admin_telegram_id": 11,
            "admin_notification_channel_id": -10099,
            **overrides,
        },
    )


def test_config_parses_allowlist_and_hides_secrets():
    config = settings()
    assert config.admin_telegram_id == 11
    assert "unit-test-token" not in repr(config)
    assert "a" * 32 not in repr(config)


@pytest.mark.parametrize(
    "override",
    [
        {"admin_telegram_id": 0},
        {"admin_notification_channel_id": 1},
        {"database_url": "sqlite:///test.db"},
        {"telegram_webhook_secret": "short"},
        {"telegram_bot_token": "invalid"},
        {"telegram_media_channel_id": 1},
        {"telegram_archive_enabled": True, "telegram_media_channel_id": None},
        {"telegram_tts_enabled": True, "telegram_archive_enabled": False},
    ],
)
def test_invalid_config_fails_early(override):
    with pytest.raises(ValidationError):
        settings(**override)


def test_private_actor_cannot_be_taken_from_group_or_other_chat():
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 0,
            "from": {"id": 11, "is_bot": False, "first_name": "Test"},
            "chat": {"id": 22, "type": "private"},
        },
    }
    assert actor(parse_update(payload), settings()) is None
    payload["message"]["chat"] = {"id": 11, "type": "group"}
    assert actor(parse_update(payload), settings()) is None


def test_structured_logger_keeps_ids_and_excludes_unapproved_fields():
    record = logging.LogRecord("test", logging.INFO, "test", 1, "update_received", (), None)
    record.update_id, record.user_id, record.token = 4, 11, "private-token"
    data = json.loads(JsonFormatter().format(record))
    assert data["event"] == "update_received" and data["update_id"] == 4
    assert "token" not in data


async def test_liveness_does_not_depend_on_database():
    class UnavailableDatabase:
        async def ready(self):
            raise OSError("Connection refused")

    app = create_app(settings(), UnavailableDatabase())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 503
        # Invalid authentication is rejected before any persistence or JSON parsing.
        assert (await client.post("/telegram/webhook", content="invalid json")).status_code == 403


async def test_fake_providers_work_without_model_dependencies(tmp_path: Path):
    providers = fake_providers()
    source = tmp_path / "source.wav"
    source.write_bytes(b"test input")
    transcription = await providers.asr.transcribe(source)
    translation = await providers.translation.translate(transcription.text, "de", "en")
    assert transcription.provider == "fake" and translation.provider == "fake"
    voices = [VoiceConfig(id=str(i), provider_voice_id=f"test-{i}") for i in range(3)]
    outputs = await asyncio.gather(
        *(
            providers.tts.synthesize(transcription.text, voice, tmp_path / "generated")
            for voice in voices
        )
    )
    assert len({output.path for output in outputs}) == 3
    for output in outputs:
        with wave.open(str(output.path)) as audio:
            assert audio.getnframes() > 0
    stored = await providers.media.save(outputs[0].path)
    outputs[0].path.unlink()
    await providers.media.send_to_user(stored, 11)
    await providers.media.delete(stored)
    await providers.media.delete(stored)
    with pytest.raises(FileNotFoundError):
        await providers.media.send_to_user(stored, 11)
