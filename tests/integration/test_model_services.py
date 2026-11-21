import asyncio
import wave
from datetime import timedelta
from app.core.idgen import generate_id

import httpx
import pytest
from sqlalchemy import func, select, update

from app.db.models import ModelJob, Operation
from app.inference.main import ModelQueue, ModelSettings
from app.inference.main import create_app as model_app
from app.providers.http import ModelClient
from app.services.operations import OperationRunner

from .test_foundation import RecordingTransport, dispatch, payload
from .test_operations import VOICE


def fake_infer(job, output):
    if job.kind == "tts":
        with wave.open(str(output), "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\x00\x00" * 160)
        return None, str(output)
    return ("Guten Morgen." if job.kind == "asr" else "Good morning."), None


def service(db, kind, tmp_path, monkeypatch):
    monkeypatch.setenv("LANGUAGE_BOT_DATA_DIR", str(tmp_path))
    settings = ModelSettings(database_url="postgresql+asyncpg://unused/test", model_kind=kind)
    return model_app(settings, db, infer=fake_infer, run_worker=False)


@pytest.mark.parametrize("kind", ["asr", "translation", "tts"])
async def test_service_persistent_idempotent_jobs_and_cleanup(db, tmp_path, monkeypatch, kind):
    app = service(db, kind, tmp_path, monkeypatch)
    job_id = generate_id()
    url = f"/jobs/{job_id}"
    kwargs = {"content": b"test audio"} if kind == "asr" else {"json": {"text": "Guten Morgen."}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model"
    ) as client:
        assert (await client.put(url, **kwargs)).status_code == 202
        assert (await client.put(url, **kwargs)).status_code == 202
        conflict = {"content": b"different"} if kind == "asr" else {"json": {"text": "different"}}
        assert (await client.put(url, **conflict)).status_code == 409
        assert (await client.delete(url)).status_code == 409
        # A new queue instance claims the persisted job without any original request object.
        queue = ModelQueue(db, app.state.queue.settings, fake_infer)
        assert await queue.process_one()
        result = (await client.get(url)).json()
        assert result["state"] == "done"
        if kind == "tts":
            assert (await client.get(url + "/audio")).content[:4] == b"RIFF"
        else:
            assert result["text"]
        assert (await client.delete(url)).status_code == 204
        assert (await client.put(url, **kwargs)).json()["state"] == "done"
    async with db.sessions() as session:
        row = await session.get(ModelJob, (job_id, kind))
        assert row.input_path is None and row.output_path is None


async def test_queue_backpressure_and_validation(db, tmp_path, monkeypatch):
    app = service(db, "translation", tmp_path, monkeypatch)
    app.state.queue.settings.model_queue_limit = 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model"
    ) as client:
        assert (await client.put(f"/jobs/{generate_id()}", json={"text": "hello"})).status_code == 202
        assert (await client.put(f"/jobs/{generate_id()}", json={"text": "second"})).status_code == 429
        assert (await client.put(f"/jobs/{generate_id()}", json={"text": " " * 5})).status_code == 422
        assert (await client.put(f"/jobs/{generate_id()}", json={"text": "x" * 10001})).status_code == 422


async def test_model_lease_recovery_and_late_writer_fencing(db, tmp_path, monkeypatch):
    app = service(db, "asr", tmp_path, monkeypatch)
    queue = app.state.queue
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model"
    ) as client:
        await client.put(f"/jobs/{generate_id()}", content=b"audio")
    old = await queue.claim()
    assert await queue.claim() is None
    async with db.sessions.begin() as session:
        await session.execute(
            update(ModelJob).values(lease_until=func.now() - timedelta(seconds=1))
        )
    new = await queue.claim()
    assert not await queue.finish(old, state="done", output_text="stale")
    assert await queue.finish(new, state="done", output_text="current")


async def test_cpu_work_does_not_block_model_api(db, tmp_path, monkeypatch):
    import threading

    entered, release = threading.Event(), threading.Event()
    app = service(db, "translation", tmp_path, monkeypatch)

    def blocking(job, output):
        entered.set()
        release.wait(timeout=5)
        return "done", None

    app.state.queue.infer = blocking
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model"
    ) as client:
        job_id = generate_id()
        await client.put(f"/jobs/{job_id}", json={"text": "hello"})
        task = asyncio.create_task(app.state.queue.process_one())
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            async with asyncio.timeout(1):
                assert (await client.get("/health/ready")).status_code == 200
                assert (await client.get(f"/jobs/{job_id}")).json()["state"] == "running"
        finally:
            release.set()
            await task


async def test_bot_pipeline_through_real_http_contracts(db, config, tmp_path, monkeypatch):
    apps = {
        kind: service(db, kind, tmp_path, monkeypatch) for kind in ["asr", "translation", "tts"]
    }

    class Routed(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return await httpx.ASGITransport(app=apps[request.url.host]).handle_async_request(
                request
            )

    client = httpx.AsyncClient(transport=Routed())
    models = ModelClient(config, client=client)
    tasks = [asyncio.create_task(app.state.queue.serve()) for app in apps.values()]
    try:
        transport = RecordingTransport()
        await dispatch(db, config, transport, payload(voice=VOICE))
        runner = OperationRunner(db, config, transport, models)
        async with asyncio.timeout(10):
            assert await runner.process_one()
            assert await runner.process_one()
            assert await runner.process_one()
        async with db.sessions() as session:
            operation = await session.scalar(select(Operation))
            assert operation.state == "done" and operation.english == "Good morning."
            jobs = (await session.scalars(select(ModelJob))).all()
            assert {job.kind for job in jobs} == {"asr", "translation"}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.aclose()


async def test_cleanup_removes_expired_files_but_keeps_receipts(db, tmp_path, monkeypatch):
    app = service(db, "asr", tmp_path, monkeypatch)
    job_id = generate_id()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model"
    ) as client:
        await client.put(f"/jobs/{job_id}", content=b"audio")
        await app.state.queue.process_one()
    async with db.sessions.begin() as session:
        job = await session.get(ModelJob, (job_id, "asr"))
        path = job.input_path
        job.created_at = func.now() - timedelta(days=2)
    await app.state.queue.cleanup()
    from pathlib import Path

    assert not Path(path).exists()
    async with db.sessions() as session:
        job = await session.get(ModelJob, (job_id, "asr"))
        assert job.state == "done" and job.output_text == "Guten Morgen." and job.input_path is None


async def test_asr_restart_reuses_result_without_redownloading_telegram_audio(
    db, config, tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from app.core.idgen import derive_id

    app = service(db, "asr", tmp_path, monkeypatch)
    operation = SimpleNamespace(id=generate_id(), generation=0, source_file_id="unavailable")
    job_id = derive_id("asr", operation.id, 0)

    class UnavailableTelegram:
        async def download(self, file_id):
            raise AssertionError("Completed model jobs must not need the original input again")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://asr:8080"
    ) as client:
        await client.put(f"/jobs/{job_id}", content=b"audio")
        await app.state.queue.process_one()
        assert (
            await ModelClient(config, client).transcribe(operation, UnavailableTelegram())
            == "Guten Morgen."
        )


async def test_transient_model_failure_retries_and_validation_failure_stops(
    db, tmp_path, monkeypatch
):
    app = service(db, "translation", tmp_path, monkeypatch)
    calls = []

    def transient(job, output):
        calls.append(job.id)
        if len(calls) == 1:
            raise OSError("temporary")
        return "translated", None

    app.state.queue.infer = transient
    job_id = generate_id()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model"
    ) as client:
        await client.put(f"/jobs/{job_id}", json={"text": "Hallo"})
        await app.state.queue.process_one()
        assert (await client.get(f"/jobs/{job_id}")).json()["state"] == "pending"
        async with db.sessions.begin() as session:
            (await session.get(ModelJob, (job_id, "translation"))).available_at = (
                func.now() - timedelta(seconds=1)
            )
        await app.state.queue.process_one()
        assert (await client.get(f"/jobs/{job_id}")).json()["text"] == "translated"

        def invalid(job, output):
            raise ValueError("InvalidInput")

        app.state.queue.infer = invalid
        failed_id = generate_id()
        await client.put(f"/jobs/{failed_id}", json={"text": "bad"})
        await app.state.queue.process_one()
        assert (await client.get(f"/jobs/{failed_id}")).json()["state"] == "failed"
        assert not await app.state.queue.process_one()
