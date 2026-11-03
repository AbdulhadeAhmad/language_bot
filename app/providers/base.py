from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class ProviderResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider: str
    model: str


class TranscriptionResult(ProviderResult):
    text: str
    language: str


class TranslationResult(ProviderResult):
    text: str
    source_language: str
    target_language: str


class VoiceConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    provider_voice_id: str
    language: str = "de"
    settings: dict = Field(default_factory=dict)


class GeneratedAudio(ProviderResult):
    path: Path
    voice_id: str
    mime_type: str


class StoredMedia(BaseModel):
    model_config = ConfigDict(frozen=True)
    backend: str
    key: str
    metadata: dict = Field(default_factory=dict)


class ASRProvider(Protocol):
    async def transcribe(self, audio_path: Path, language: str = "de") -> TranscriptionResult: ...


class TranslationProvider(Protocol):
    async def translate(
        self, text: str, source_language: str, target_language: str
    ) -> TranslationResult: ...


class TTSProvider(Protocol):
    async def synthesize(
        self, text: str, voice: VoiceConfig, output_dir: Path
    ) -> GeneratedAudio: ...


class MediaStore(Protocol):
    async def save(self, audio_path: Path) -> StoredMedia: ...
    async def send_to_user(self, media: StoredMedia, user_id: int) -> None: ...
    async def delete(self, media: StoredMedia) -> None: ...


@dataclass(frozen=True)
class Providers:
    asr: ASRProvider
    translation: TranslationProvider
    tts: TTSProvider
    media: MediaStore
