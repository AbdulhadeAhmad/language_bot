import sys
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.transport import BotTransport, RetryAfter, UploadedAudio, delivery_error
from app.inference.runtime import load, speech_chunks


def test_speech_chunks_handle_long_transcripts_without_losing_text():
    text = "Grüße aus Berlin. " * 500
    chunks = list(speech_chunks(text))
    assert len(chunks) > 1 and all(0 < len(chunk) <= 1000 for chunk in chunks)
    assert " ".join(chunks) == " ".join(text.split())
    assert "".join(speech_chunks("x" * 1001)) == "x" * 1001


def test_piper_chunks_share_one_valid_wav_header(monkeypatch, tmp_path):
    calls = []

    class Piper:
        @classmethod
        def load(cls, *args, **kwargs):
            return cls()

        def synthesize_wav(self, text, stream, set_wav_format):
            calls.append((text, set_wav_format))
            if set_wav_format:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\x00\x00" * 160)

    monkeypatch.setitem(sys.modules, "piper", SimpleNamespace(PiperVoice=Piper))
    infer = load("tts", tmp_path, 1)
    output = tmp_path / "speech.wav"
    assert infer(SimpleNamespace(input_text="Guten Morgen. " * 200), output) == (None, str(output))
    assert calls[0][1] and all(not first for _, first in calls[1:])
    with wave.open(str(output)) as stream:
        assert stream.getnframes() == len(calls) * 160


async def test_generated_upload_uses_mp3_and_releases_memory_buffer():
    transport = BotTransport("12345:test")
    streams = []

    async def send(channel, upload, **kwargs):
        assert channel == -10088 and kwargs["caption"] == "archive metadata"
        assert kwargs["title"] == "Guten Morgen." and kwargs["performer"] == "Thorsten Medium"
        assert kwargs["disable_notification"]
        streams.append(upload.file)
        assert upload.file.read() == b"mp3 audio"
        return SimpleNamespace(message_id=7, audio=SimpleNamespace(file_id="saved-audio"))

    transport.bot.send_audio = AsyncMock(side_effect=send)
    assert await transport.upload_audio(
        -10088,
        b"mp3 audio",
        caption="archive metadata",
        title="Guten Morgen.",
        performer="Thorsten Medium",
        file_name="german-de_DE-thorsten-medium.mp3",
    ) == UploadedAudio(7, "saved-audio")
    assert streams[0].closed
    await transport.close()


def test_audio_rate_limit_preserves_retry_after():
    error = SimpleNamespace(error_code=429, result_json={"parameters": {"retry_after": 45}})
    with pytest.raises(RetryAfter) as caught:
        delivery_error(error)
    assert caught.value.seconds == 45


def test_archive_metadata_keeps_ids_when_long_german_is_shortened():
    from uuid import uuid4

    from app.services.audio import archive_audio_metadata

    sentence = SimpleNamespace(id=uuid4(), user_id=12345, revision=2, german="Grüße 😊 " * 2000)
    asset = SimpleNamespace(id=uuid4())
    voice = SimpleNamespace(id="de_DE-thorsten-medium", provider="piper")
    metadata = archive_audio_metadata(sentence, asset, voice)
    assert len(metadata["caption"].encode("utf-16-le")) // 2 <= 1024
    assert f"Sentence ID: {sentence.id}" in metadata["caption"]
    assert "User ID: 12345" in metadata["caption"] and "Revision: 2" in metadata["caption"]
    assert str(asset.id) in metadata["caption"] and "de_DE-thorsten-medium" in metadata["caption"]
    assert "…" in metadata["caption"]
    assert "12345" not in metadata["title"] + metadata["performer"] + metadata["file_name"]
