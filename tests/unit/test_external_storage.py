import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from telebot.types import InlineKeyboardMarkup

from app.bot.transport import BotTransport
from app.paths import data_root


def test_data_root_is_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGUAGE_BOT_DATA_DIR", str(tmp_path))
    assert data_root() == tmp_path
    monkeypatch.setenv("LANGUAGE_BOT_DATA_DIR", "relative")
    with pytest.raises(ValueError):
        data_root()


def test_artifact_migration_preserves_originals_and_rewrites_live_manifest(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "migration", Path(__file__).resolve().parents[2] / "scripts/migrate_artifacts.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    old, new = tmp_path / "repo/artifacts", tmp_path / "data"
    (old / "hub").mkdir(parents=True)
    (old / "hub/model").write_bytes(b"model bytes")
    (old / "voices").mkdir()
    (old / "voices/test").symlink_to(old / "hub/model")
    manifest = {"test": {"path": str(old / "voices/test")}}
    (old / "models.json").write_text(json.dumps(manifest))
    (old / "results").mkdir()
    (old / "results/report.json").write_text("{}")
    module.migrate(old, new)
    module.migrate(old, new)
    assert json.loads((old / "models.json").read_text()) == manifest
    assert json.loads((new / "models/models.json").read_text())["test"]["path"] == str(
        new / "models/voices/test"
    )
    assert not (new / "models/voices/test").is_symlink()
    assert (new / "benchmarks/results/report.json").is_file()
    (new / "models/hub/model").write_bytes(b"other")
    with pytest.raises(ValueError):
        module.migrate(old, new)


async def test_transport_restores_native_keyboard():
    transport = BotTransport("12345:test")
    transport.bot.send_message = AsyncMock(return_value=type("Message", (), {"message_id": 1})())
    assert (
        await transport.send(
            "send_message", {"chat_id": 11, "text": "test", "reply_markup": {"inline_keyboard": []}}
        )
        == 1
    )
    assert isinstance(
        transport.bot.send_message.call_args.kwargs["reply_markup"], InlineKeyboardMarkup
    )
    await transport.close()


async def test_archive_transport_copies_without_forwarding_identity_or_caption():
    from app.services.archive import copy_payload

    transport = BotTransport("12345:test")
    transport.bot.copy_message = AsyncMock(return_value=type("Receipt", (), {"message_id": 7})())
    assert await transport.send("copy_message", copy_payload(-10088, 11, 42)) == 7
    kwargs = transport.bot.copy_message.call_args.kwargs
    assert kwargs["caption"] == "" and kwargs["disable_notification"]
    assert kwargs["from_chat_id"] == 11 and kwargs["message_id"] == 42
    assert isinstance(kwargs["reply_markup"], InlineKeyboardMarkup)
    await transport.close()
