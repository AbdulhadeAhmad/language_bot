"""Async clients for the model APIs. Stable IDs resume existing durable model jobs."""

import asyncio

import httpx

from app.core.idgen import derive_id
from app.voice_catalog import DEFAULT_VOICE_ID


class ModelClient:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=30)

    async def run(self, kind, job_id, *, audio=None, text=None, voice_id=None):
        url = getattr(self.settings, f"{kind}_url").rstrip("/") + f"/jobs/{job_id}"
        payload = {"text": text}
        if voice_id:
            payload["voice_id"] = voice_id
        response = (
            await self.client.put(
                url, content=audio, headers={"Content-Type": "application/octet-stream"}
            )
            if audio is not None
            else await self.client.put(url, json=payload)
        )
        response.raise_for_status()
        return await self.wait(url)

    async def wait(self, url):
        while True:
            response = await self.client.get(url)
            response.raise_for_status()
            result = response.json()
            if result["state"] == "done":
                return result
            if result["state"] == "failed":
                raise ValueError("Model inference failed")
            await asyncio.sleep(0.5)

    async def transcribe(self, operation, transport):
        job_id = derive_id("asr", operation.id, operation.generation)
        url = self.settings.asr_url.rstrip("/") + f"/jobs/{job_id}"
        existing = await self.client.get(url)
        if existing.status_code != 404:
            existing.raise_for_status()
            return (await self.wait(url))["text"]
        # Telegram references are domain input; raw Telegram messages never leave the API.
        audio = await transport.download(operation.source_file_id)
        result = await self.run("asr", job_id, audio=audio)
        return result["text"]

    async def translate(self, operation):
        job_id = derive_id("translation", operation.id, operation.generation)
        result = await self.run("translation", job_id, text=operation.german)
        return result["text"]

    @staticmethod
    def speech_id(job):
        return derive_id("tts", job.asset_id, job.generation)

    async def synthesize(self, job, german, voice_id=DEFAULT_VOICE_ID):
        return await self.run("tts", self.speech_id(job), text=german, voice_id=voice_id)

    async def speech_audio(self, job):
        url = self.settings.tts_url.rstrip("/") + f"/jobs/{self.speech_id(job)}/audio?format=mp3"
        async with self.client.stream("GET", url) as response:
            response.raise_for_status()
            audio = bytearray()
            async for chunk in response.aiter_bytes():
                audio.extend(chunk)
                if len(audio) > 20 * 1024 * 1024:
                    raise ValueError("GeneratedAudioTooLarge")
        if not audio:
            raise ValueError("EmptyGeneratedAudio")
        return bytes(audio)

    async def discard_speech(self, job):
        url = self.settings.tts_url.rstrip("/") + f"/jobs/{self.speech_id(job)}"
        response = await self.client.delete(url)
        response.raise_for_status()

    async def close(self):
        await self.client.aclose()
