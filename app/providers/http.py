"""Async clients for the model APIs. Stable IDs resume existing durable model jobs."""

import asyncio
from uuid import uuid5

import httpx


class ModelClient:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=30)

    async def run(self, kind, job_id, *, audio=None, text=None):
        url = getattr(self.settings, f"{kind}_url").rstrip("/") + f"/jobs/{job_id}"
        response = (
            await self.client.put(
                url, content=audio, headers={"Content-Type": "application/octet-stream"}
            )
            if audio is not None
            else await self.client.put(url, json={"text": text})
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
        job_id = uuid5(operation.id, f"asr:{operation.generation}")
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
        job_id = uuid5(operation.id, f"translation:{operation.generation}")
        result = await self.run("translation", job_id, text=operation.german)
        return result["text"]

    async def close(self):
        await self.client.aclose()
