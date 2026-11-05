"""Private model API with a durable PostgreSQL queue and one CPU consumer per service."""

import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import and_, func, or_, select, text, update

from app.db.models import ModelJob
from app.db.session import Database
from app.inference.runtime import encode_mp3, load
from app.paths import data_root

logger = logging.getLogger(__name__)


class ModelSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    database_url: SecretStr
    model_kind: str
    model_dir: Path = Path("/models")
    model_threads: int = Field(default=1, ge=1, le=4)
    model_queue_limit: int = Field(default=32, ge=1, le=1000)
    model_poll_seconds: float = 0.5


class TextInput(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class ModelQueue:
    def __init__(self, db, settings, infer):
        self.db, self.settings, self.infer = db, settings, infer
        self.directory = data_root() / "jobs" / settings.model_kind
        self.directory.mkdir(parents=True, exist_ok=True)
        self.last_cleanup = 0.0

    async def claim(self):
        async with self.db.sessions.begin() as session:
            job = await session.scalar(
                select(ModelJob)
                .where(
                    ModelJob.kind == self.settings.model_kind,
                    ModelJob.available_at <= func.now(),
                    or_(
                        ModelJob.state == "pending",
                        and_(ModelJob.state == "running", ModelJob.lease_until < func.now()),
                    ),
                )
                .order_by(ModelJob.available_at, ModelJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            job.state, job.claim_token = "running", uuid4()
            job.lease_until = func.now() + timedelta(seconds=60)
            job.attempts += 1
            await session.flush()
            return job

    async def finish(self, job, **values):
        async with self.db.sessions.begin() as session:
            result = await session.execute(
                update(ModelJob)
                .where(
                    ModelJob.id == job.id,
                    ModelJob.kind == job.kind,
                    ModelJob.claim_token == job.claim_token,
                    ModelJob.state == "running",
                )
                .values(claim_token=None, lease_until=None, **values)
            )
            return result.rowcount == 1

    async def heartbeat(self, job):
        while True:
            await asyncio.sleep(10)
            async with self.db.sessions.begin() as session:
                await session.execute(
                    update(ModelJob)
                    .where(
                        ModelJob.id == job.id,
                        ModelJob.kind == job.kind,
                        ModelJob.claim_token == job.claim_token,
                        ModelJob.state == "running",
                    )
                    .values(lease_until=func.now() + timedelta(seconds=60))
                )

    async def process_one(self):
        job = await self.claim()
        if job is None:
            return False
        heartbeat = asyncio.create_task(self.heartbeat(job))
        output = self.directory / f"{job.id}-{job.claim_token}.wav"
        try:
            if job.attempts > 3:
                raise ValueError("RetryLimit")
            result_text, result_path = await asyncio.to_thread(self.infer, job, output)
            accepted = await self.finish(
                job, state="done", output_text=result_text, output_path=result_path, error_type=None
            )
            if not accepted:
                output.unlink(missing_ok=True)
        except Exception as exc:
            output.unlink(missing_ok=True)
            await self.finish(
                job,
                state="failed" if isinstance(exc, ValueError) or job.attempts >= 3 else "pending",
                error_type=type(exc).__name__[:100],
                available_at=func.now() + timedelta(seconds=2**job.attempts),
            )
            logger.warning("model_job_failed: %s", type(exc).__name__)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def cleanup(self):
        # Artifacts are temporary. Completed receipts remain for idempotent retries.
        async with self.db.sessions.begin() as session:
            rows = (
                await session.scalars(
                    select(ModelJob)
                    .where(
                        ModelJob.kind == self.settings.model_kind,
                        ModelJob.state.in_(["done", "failed"]),
                        ModelJob.created_at < func.now() - timedelta(hours=24),
                        or_(ModelJob.input_path.is_not(None), ModelJob.output_path.is_not(None)),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(100)
                )
            ).all()
            for row in rows:
                for value in (row.input_path, row.output_path):
                    if value:
                        Path(value).unlink(missing_ok=True)
                row.input_path, row.output_path, row.input_text = None, None, None
        async with self.db.sessions() as session:
            rows = (
                await session.execute(
                    select(ModelJob.input_path, ModelJob.output_path).where(
                        ModelJob.kind == self.settings.model_kind
                    )
                )
            ).all()
            referenced = {value for row in rows for value in row if value}

        def remove_orphans():
            cutoff = time.time() - 86400
            for path in self.directory.iterdir():
                if str(path) not in referenced and path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)

        await asyncio.to_thread(remove_orphans)

    async def serve(self):
        while True:
            try:
                if time.monotonic() - self.last_cleanup > 60:
                    await self.cleanup()
                    self.last_cleanup = time.monotonic()
                worked = await self.process_one()
            except Exception as exc:
                logger.error("model_queue_error: %s", type(exc).__name__)
                worked = False
            if not worked:
                await asyncio.sleep(self.settings.model_poll_seconds)


def create_app(settings=None, database=None, infer=None, run_worker=True):
    settings = settings or ModelSettings()
    if settings.model_kind not in {"asr", "translation", "tts"}:
        raise ValueError("Invalid MODEL_KIND")
    owned = database is None
    database = database or Database(settings.database_url.get_secret_value())
    queue = ModelQueue(database, settings, infer)

    @asynccontextmanager
    async def lifespan(app):
        if not await database.ready():
            raise RuntimeError("Apply migrations first")
        if queue.infer is None:
            queue.infer = await asyncio.to_thread(
                load, settings.model_kind, settings.model_dir, settings.model_threads
            )
        task = asyncio.create_task(queue.serve()) if run_worker else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if owned:
                await database.close()

    app = FastAPI(title=f"{settings.model_kind} model API", lifespan=lifespan)
    app.state.queue = queue

    @app.get("/health/ready")
    async def ready():
        if queue.infer is None or not await database.ready():
            raise HTTPException(503, "Not ready")
        return {"status": "ready", "kind": settings.model_kind}

    @app.put("/jobs/{job_id}", status_code=202)
    async def submit(job_id: UUID, request: Request):
        body = bytearray()
        limit = 20 * 1024 * 1024 if settings.model_kind == "asr" else 80000
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                raise HTTPException(413, "Input too large")
        input_text = None
        if settings.model_kind != "asr":
            try:
                input_text = TextInput.model_validate_json(body).text.strip()
                if not input_text:
                    raise ValueError()
            except ValueError:
                raise HTTPException(422, "Expected non-empty text within service limits") from None
        elif not body:
            raise HTTPException(422, "Audio required")
        fingerprint = hashlib.sha256(
            body if input_text is None else input_text.encode()
        ).hexdigest()
        async with database.sessions.begin() as session:
            # Serialize admission only, so the persistent queue limit cannot be overrun.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": {"asr": 83101, "translation": 83102, "tts": 83103}[settings.model_kind]},
            )
            existing = await session.get(ModelJob, (job_id, settings.model_kind))
            if existing:
                if existing.fingerprint != fingerprint:
                    raise HTTPException(409, "Job ID already used for different input")
                return {"id": str(existing.id), "state": existing.state}
            count = await session.scalar(
                select(func.count())
                .select_from(ModelJob)
                .where(
                    ModelJob.kind == settings.model_kind, ModelJob.state.in_(["pending", "running"])
                )
            )
            if count >= settings.model_queue_limit:
                raise HTTPException(429, "Model queue full", headers={"Retry-After": "5"})
            input_path = None
            if input_text is None:
                path = queue.directory / f"{job_id}.input"
                temporary = path.with_suffix(f".{uuid4()}.tmp")
                await asyncio.to_thread(temporary.write_bytes, body)
                temporary.replace(path)
                input_path = str(path)
            session.add(
                ModelJob(
                    id=job_id,
                    kind=settings.model_kind,
                    fingerprint=fingerprint,
                    input_text=input_text,
                    input_path=input_path,
                )
            )
        return {"id": str(job_id), "state": "pending"}

    @app.get("/jobs/{job_id}")
    async def result(job_id: UUID):
        async with database.sessions() as session:
            job = await session.get(ModelJob, (job_id, settings.model_kind))
            if not job:
                raise HTTPException(404, "Job not found")
            return {
                "id": str(job.id),
                "state": job.state,
                "text": job.output_text,
                "audio_ready": bool(job.output_path),
                "error_type": job.error_type,
            }

    @app.get("/jobs/{job_id}/audio")
    async def audio(job_id: UUID, format: Literal["wav", "mp3"] = "wav"):
        async with database.sessions() as session:
            job = await session.get(ModelJob, (job_id, settings.model_kind))
            if not job or job.state != "done" or not job.output_path:
                raise HTTPException(404, "Audio not ready")
            path = job.output_path
        if not Path(path).is_file():
            raise HTTPException(404, "Audio not ready")
        if format == "mp3":
            return Response(await asyncio.to_thread(encode_mp3, path), media_type="audio/mpeg")
        return FileResponse(path, media_type="audio/wav")

    @app.delete("/jobs/{job_id}", status_code=204)
    async def discard(job_id: UUID):
        async with database.sessions.begin() as session:
            job = await session.scalar(
                select(ModelJob)
                .where(ModelJob.id == job_id, ModelJob.kind == settings.model_kind)
                .with_for_update()
            )
            if job:
                if job.state in {"pending", "running"}:
                    raise HTTPException(409, "Job still active")
                # Keep a completed receipt for retry idempotency but release intermediate data.
                for path in (job.input_path, job.output_path):
                    if path:
                        Path(path).unlink(missing_ok=True)
                job.input_path, job.output_path, job.input_text = None, None, None
        return None

    return app


def run():
    import uvicorn

    uvicorn.run("app.inference.main:create_app", factory=True, host="0.0.0.0", port=8080)
