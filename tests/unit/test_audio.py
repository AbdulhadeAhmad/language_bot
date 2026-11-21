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
    assert infer(SimpleNamespace(input_text="Guten Morgen. " * 200, voice_id="de_DE-thorsten-medium"), output) == (None, str(output))
    assert calls[0][1] and all(not first for _, first in calls[1:])
    with wave.open(str(output)) as stream:
        assert stream.getnframes() == len(calls) * 160


async def test_generated_upload_uses_mp3_and_releases_memory_buffer():
    transport = BotTransport("12345:test")
    streams = []

    async def send(channel, upload, **kwargs):
        assert channel == -10088 and kwargs["caption"] == "archive metadata"
        assert kwargs["title"] == "Guten Morgen." and kwargs["performer"] == "Thorsten Medium"
        assert kwargs["parse_mode"] == "HTML"
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
        file_name="Guten Morgen.mp3",
        parse_mode="HTML",
    ) == UploadedAudio(7, "saved-audio")
    assert streams[0].closed
    await transport.close()


def test_audio_rate_limit_preserves_retry_after():
    error = SimpleNamespace(error_code=429, result_json={"parameters": {"retry_after": 45}})
    with pytest.raises(RetryAfter) as caught:
        delivery_error(error)
    assert caught.value.seconds == 45


def test_archive_metadata_keeps_ids_when_long_german_is_shortened():
    import re
    from html import unescape
    from app.core.idgen import generate_id

    from app.services.audio import archive_audio_metadata

    sentence = SimpleNamespace(
        id=generate_id(),
        user_id=12345,
        revision=2,
        german="Grüße 😊 " * 2000,
        english="Greetings <from> Berlin & elsewhere. " * 1000,
    )
    asset = SimpleNamespace(id=generate_id())
    voice = SimpleNamespace(id="de_DE-thorsten-medium", provider="piper")
    user = SimpleNamespace(public_id=generate_id())
    metadata = archive_audio_metadata(sentence, asset, voice, user)
    visible_caption = unescape(re.sub(r"</?b>", "", metadata["caption"]))
    assert len(visible_caption.encode("utf-16-le")) // 2 <= 1024
    assert f"#ST_{sentence.id}" in metadata["caption"]
    assert f"#UR_{user.public_id}" in metadata["caption"] and "#revision_2" in metadata["caption"]
    assert f"#AU_{asset.id}" in metadata["caption"] and "#de_DE_thorsten_medium" in metadata["caption"]
    assert "<b>English</b>" in metadata["caption"] and "&lt;from&gt;" in metadata["caption"]
    assert "&amp;" in metadata["caption"] and "#piper" in metadata["caption"]
    assert metadata["parse_mode"] == "HTML"
    assert "…" in metadata["caption"]
    assert "12345" not in metadata["title"] + metadata["performer"] + metadata["file_name"]


def test_archive_labels_use_german_filename_and_escape_user_text():
    from app.core.idgen import generate_id

    from app.services.audio import archive_audio_metadata

    sentence = SimpleNamespace(
        id=generate_id(),
        user_id=12,
        revision=1,
        german='Grüße <aus> Berlin & "Welt".',
        english="Greetings from Berlin & the world.",
    )
    metadata = archive_audio_metadata(
        sentence,
        SimpleNamespace(id=generate_id()),
        SimpleNamespace(id="de_DE-thorsten-medium", provider="piper"),
        SimpleNamespace(public_id=generate_id()),
    )
    assert metadata["title"] == sentence.german
    assert metadata["file_name"] == "Grüße aus Berlin & Welt.mp3"
    assert "&lt;aus&gt;" in metadata["caption"] and "&quot;Welt&quot;" in metadata["caption"]
    assert "Greetings from Berlin &amp; the world." in metadata["caption"]
