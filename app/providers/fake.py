"""Explicit test doubles, never silently substituted for real model providers."""

import asyncio
import hashlib
import wave
from pathlib import Path

from app.providers.base import (
    GeneratedAudio,
    Providers,
    StoredMedia,
    TranscriptionResult,
    TranslationResult,
    VoiceConfig,
)


class FakeASR:
    async def transcribe(self, audio_path: Path, language: str = "de") -> TranscriptionResult:
        if not audio_path.is_file():
            raise FileNotFoundError(audio_path)
        return TranscriptionResult(
            text="Guten Morgen.", language=language, provider="fake", model="test"
        )


class FakeTranslation:
    async def translate(
        self, text: str, source_language: str, target_language: str
    ) -> TranslationResult:
        return TranslationResult(
            text=f"[fake {target_language}] {text}",
            source_language=source_language,
            target_language=target_language,
            provider="fake",
            model="test",
        )


class FakeTTS:
    async def synthesize(self, text: str, voice: VoiceConfig, output_dir: Path) -> GeneratedAudio:
        def write():
            output_dir.mkdir(parents=True, exist_ok=True)
            # Digest-based paths cannot escape the caller-owned temporary directory.
            name = hashlib.sha256(f"{voice.id}:{text}".encode()).hexdigest()
            path = output_dir / f"{name}.wav"
            with wave.open(str(path), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(16000)
                stream.writeframes(b"\0\0" * 1600)
            return path

        return GeneratedAudio(
            path=await asyncio.to_thread(write),
            voice_id=voice.id,
            mime_type="audio/wav",
            provider="fake",
            model="test",
        )


class FakeMediaStore:
    def __init__(self) -> None:
        self.assets: dict[str, bytes] = {}
        self.sent: list[tuple[str, int]] = []

    async def save(self, audio_path: Path) -> StoredMedia:
        data = await asyncio.to_thread(audio_path.read_bytes)
        key = hashlib.sha256(data).hexdigest()
        self.assets[key] = data
        return StoredMedia(backend="fake", key=key)

    async def send_to_user(self, media: StoredMedia, user_id: int) -> None:
        if media.key not in self.assets:
            raise FileNotFoundError(media.key)
        self.sent.append((media.key, user_id))

    async def delete(self, media: StoredMedia) -> None:
        self.assets.pop(media.key, None)


def fake_providers() -> Providers:
    return Providers(FakeASR(), FakeTranslation(), FakeTTS(), FakeMediaStore())
