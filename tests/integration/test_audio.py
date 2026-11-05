import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
from sqlalchemy import func, select

from app.bot.transport import UploadedAudio
from app.db.models import AudioAsset, AudioJob, ModelJob, Sentence, StoredMedia, User
from app.providers.http import ModelClient
from app.services.audio import AudioRunner, playback_payload, request_audio

from .test_collections import capture
from .test_foundation import RecordingTransport, dispatch, payload
from .test_model_services import service


class SpeechModels:
    def __init__(self):
        self.generated, self.discarded = [], []
        self.synthesis_error = self.audio_error = None

    async def synthesize(self, job, german):
        self.generated.append((job.asset_id, job.generation, german))
        if self.synthesis_error:
            raise self.synthesis_error

    async def speech_audio(self, job):
        if self.audio_error:
            raise self.audio_error
        return b"mp3 audio"

    async def discard_speech(self, job):
        self.discarded.append(job.asset_id)


class SpeechTransport(RecordingTransport):
    def __init__(self):
        super().__init__()
        self.uploaded, self.upload_error = [], None
        self.metadata = []

    async def upload_audio(self, channel, audio, **metadata):
        self.uploaded.append((channel, audio))
        self.metadata.append(metadata)
        if self.upload_error:
            raise self.upload_error
        return UploadedAudio(401, "generated-file")


def callback(user, data):
    return {
        "update_id": 99,
        "callback_query": {
            "id": "audio",
            "chat_instance": "test",
            "data": data,
            "from": {"id": user, "is_bot": False, "first_name": "Test"},
            "message": payload(user=user)["message"],
        },
    }


async def setup_speech(db, config):
    await capture(db, config, RecordingTransport())
    config.telegram_archive_enabled, config.telegram_tts_enabled = True, True
    config.telegram_media_channel_id = -10088
    async with db.sessions() as session:
        return await session.scalar(select(Sentence))


async def request(db, config, sentence, user=11):
    async with db.sessions.begin() as session:
        return await request_audio(session, config, user, sentence.id)


async def make_available(db):
    async with db.sessions.begin() as session:
        (await session.scalar(select(AudioJob))).available_at = func.now() - timedelta(seconds=1)


async def test_generation_is_opt_in_and_duplicate_requests_share_one_asset(db, config):
    sentence = await setup_speech(db, config)
    config.telegram_tts_enabled = False
    assert await request(db, config, sentence) is None
    assert not await AudioRunner(db, config, SpeechTransport(), SpeechModels()).process_one()
    config.telegram_tts_enabled = True
    assets = await asyncio.gather(*(request(db, config, sentence) for _ in range(5)))
    assert len({asset.id for asset in assets}) == 1
    transport, models = SpeechTransport(), SpeechModels()
    await AudioRunner(db, config, transport, models).process_one()
    # A fresh worker resumes upload without synthesizing again.
    await AudioRunner(db, config, transport, models).process_one()
    assert len(models.generated) == 1 and len(transport.uploaded) == 1
    assert transport.uploaded[0][0] == -10088
    assert sentence.german in transport.metadata[0]["caption"]
    assert f"Sentence ID: {sentence.id}" in transport.metadata[0]["caption"]
    assert f"User ID: {sentence.user_id}" in transport.metadata[0]["caption"]
    assert "de_DE-thorsten-medium" in transport.metadata[0]["caption"]
    assert models.discarded == [assets[0].id]
    assert transport.sent[-1][0] == "edit_message_reply_markup"
    async with db.sessions() as session:
        job = await session.get(AudioJob, assets[0].id)
        asset = await session.get(AudioAsset, assets[0].id)
        media = await session.get(StoredMedia, asset.stored_media_id)
        assert job.state == "done" and asset.state == "ready"
        assert media.telegram_file_id == "generated-file" and media.archive_message_id == 401
        assert await session.scalar(select(func.count()).select_from(AudioAsset)) == 1
    assert (await request(db, config, sentence)).id == assets[0].id
    assert not await AudioRunner(db, config, transport, models).process_one()


async def test_audio_callbacks_check_approval_ownership_and_reuse_archive(db, config):
    sentence = await setup_speech(db, config)
    async with db.sessions.begin() as session:
        session.add(User(id=22, status="approved"))
    transport = SpeechTransport()
    await dispatch(db, config, transport, callback(22, f"generate:{sentence.id}"))
    assert transport.sent[-1][1]["text"] == "Entry unavailable."
    await dispatch(db, config, transport, callback(11, f"generate:{sentence.id}"))
    runner = AudioRunner(db, config, transport, SpeechModels())
    await runner.process_one()
    await runner.process_one()
    await dispatch(db, config, transport, payload(2, text=f"/entry {sentence.id}"))
    keyboard = transport.sent[-1][1]["reply_markup"]["inline_keyboard"]
    button = next(row[0] for row in keyboard if row[0]["text"] == "Play German audio")
    for user in [22, 33]:
        before = len(transport.sent)
        await dispatch(db, config, transport, callback(user, button["callback_data"]))
        assert all(method != "copy_message" for method, _ in transport.sent[before:])
    for _ in range(2):
        await dispatch(db, config, transport, callback(11, button["callback_data"]))
        method, data = transport.sent[-2]
        assert method == "copy_message" and data["chat_id"] == 11
        assert data["from_chat_id"] == -10088 and data["message_id"] == 401
        assert data["caption"] == ""
    assert len(transport.uploaded) == 1


async def test_uncertain_upload_retry_keeps_synthesis_and_saved_text(db, config):
    sentence = await setup_speech(db, config)
    asset = await request(db, config, sentence)
    transport, models = SpeechTransport(), SpeechModels()
    runner = AudioRunner(db, config, transport, models)
    await runner.process_one()
    transport.upload_error = TimeoutError()
    for _ in range(3):
        await runner.process_one()
        await make_available(db)
    assert not await runner.process_one()
    async with db.sessions() as session:
        job = await session.get(AudioJob, asset.id)
        assert job.state == "failed" and job.step == "upload" and job.uncertain
        assert (await session.get(Sentence, sentence.id)).english == sentence.english
        assert await session.scalar(select(StoredMedia)) is None
    transport.upload_error = None
    assert (await request(db, config, sentence)).id == asset.id
    await runner.process_one()
    assert len(models.generated) == 1 and len(models.discarded) == 1
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(StoredMedia)) == 1


async def test_failed_synthesis_manual_retry_uses_new_model_identity(db, config):
    sentence = await setup_speech(db, config)
    asset = await request(db, config, sentence)
    transport, models = SpeechTransport(), SpeechModels()
    models.synthesis_error = ValueError("model failed")
    runner = AudioRunner(db, config, transport, models)
    await runner.process_one()
    await dispatch(db, config, transport, payload(2, text=f"/entry {sentence.id}"))
    assert "Could not generate German audio" in transport.sent[-1][1]["text"]
    await request(db, config, sentence)
    models.synthesis_error = None
    await runner.process_one()
    await runner.process_one()
    assert [generation for _, generation, _ in models.generated] == [0, 1]
    assert len(transport.uploaded) == 1
    async with db.sessions() as session:
        assert (await session.get(AudioJob, asset.id)).state == "done"


async def test_expired_model_artifact_restarts_synthesis_with_new_identity(db, config):
    sentence = await setup_speech(db, config)
    asset = await request(db, config, sentence)
    transport, models = SpeechTransport(), SpeechModels()
    runner = AudioRunner(db, config, transport, models)
    await runner.process_one()
    request_ = httpx.Request("GET", "http://tts/jobs/old/audio")
    models.audio_error = httpx.HTTPStatusError(
        "expired", request=request_, response=httpx.Response(404, request=request_)
    )
    await runner.process_one()
    async with db.sessions() as session:
        job = await session.get(AudioJob, asset.id)
        assert job.state == "pending" and job.step == "synthesize" and job.generation == 1
    models.audio_error = None
    await runner.process_one()
    await runner.process_one()
    assert len(models.generated) == 2 and len(transport.uploaded) == 1


async def test_late_audio_claim_and_obsolete_revision_cannot_publish(db, config):
    sentence = await setup_speech(db, config)
    asset = await request(db, config, sentence)
    runner = AudioRunner(db, config, SpeechTransport(), SpeechModels())
    await runner.process_one()
    old = await runner.claim()
    async with db.sessions.begin() as session:
        (await session.get(AudioJob, asset.id)).lease_until = func.now() - timedelta(seconds=1)
    current = await runner.claim()
    assert not await runner.finish(old, state="done", receipt=UploadedAudio(999, "stale"))
    async with db.sessions.begin() as session:
        (await session.get(Sentence, sentence.id)).revision += 1
    assert not await runner.finish(current, state="done", receipt=UploadedAudio(401, "obsolete"))
    async with db.sessions() as session:
        assert await session.scalar(select(StoredMedia)) is None
        assert not (await session.get(AudioAsset, asset.id)).active
        assert (await session.get(AudioJob, asset.id)).state == "cancelled"
    replacement = await request(db, config, sentence)
    assert replacement.id != asset.id


async def test_tts_model_http_mp3_download_and_cleanup_contract(db, config, tmp_path, monkeypatch):
    import wave

    app = service(db, "tts", tmp_path, monkeypatch)

    def encode(path):
        with wave.open(str(path)) as stream:
            assert stream.getnframes() > 0
        return b"mp3 audio"

    monkeypatch.setattr("app.inference.main.encode_mp3", encode)
    sentence = await setup_speech(db, config)
    await request(db, config, sentence)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://tts:8080"
    ) as client:
        models = ModelClient(config, client)
        runner = AudioRunner(db, config, SpeechTransport(), models)
        worker = asyncio.create_task(app.state.queue.serve())
        try:
            async with asyncio.timeout(10):
                await runner.process_one()
                await runner.process_one()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        async with db.sessions() as session:
            job = await session.scalar(select(AudioJob))
            model = await session.get(ModelJob, (models.speech_id(job), "tts"))
            assert job.state == "done" and model.output_path is None
        assert list((tmp_path / "jobs/tts").iterdir()) == []
        # Long voice transcriptions are accepted by the TTS contract.
        response = await client.put(f"/jobs/{uuid4()}", json={"text": "Guten Morgen. " * 200})
        assert response.status_code == 202


async def test_archived_original_playback_uses_reference_without_regeneration(db, config):
    from app.services.archive import ArchiveRunner

    from .test_operations import VOICE

    await capture(db, config, RecordingTransport(), voice=VOICE)
    config.telegram_archive_enabled, config.telegram_media_channel_id = True, -10088
    transport = RecordingTransport()
    await ArchiveRunner(db, config, transport).process_one()
    async with db.sessions() as session:
        asset = await session.scalar(select(AudioAsset))
        assert await playback_payload(session, 22, asset.id) is None
        data = await playback_payload(session, 11, asset.id)
        assert data["from_chat_id"] == -10088 and data["chat_id"] == 11
    await dispatch(db, config, transport, callback(11, f"play:{asset.id}"))
    assert transport.sent[-2][0] == "copy_message"
