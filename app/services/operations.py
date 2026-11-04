"""Durable domain operations. Only IDs, step inputs/results and recovery state live here."""

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Operation, User

logger = logging.getLogger(__name__)
LEASE_SECONDS = 60


async def create_operation(session, user_id, chat_id, message_id, file_id, german):
    operation_id = uuid4()
    inserted = await session.scalar(
        insert(Operation)
        .values(
            id=operation_id,
            user_id=user_id,
            chat_id=chat_id,
            source_message_id=message_id,
            source_file_id=file_id,
            german=german,
            step="transcribe" if file_id else "translate",
            status_send_state="sending",
        )
        .on_conflict_do_nothing(constraint="uq_operation_source")
        .returning(Operation.id)
    )
    operation = await session.scalar(
        select(Operation).where(
            Operation.chat_id == chat_id, Operation.source_message_id == message_id
        )
    )
    return operation, inserted is not None


async def retry_operation(session, operation_id, user_id):
    operation = await session.scalar(
        select(Operation)
        .where(Operation.id == operation_id, Operation.user_id == user_id)
        .with_for_update()
    )
    if operation is None or operation.state not in {"failed", "cancelled"}:
        return None
    operation.generation += 1
    operation.state = "pending" if operation.status_message_id else "initializing"
    operation.status_send_state = "sent" if operation.status_message_id else "sending"
    operation.attempts, operation.error_type = 0, None
    operation.available_at, operation.claim_token, operation.lease_until = func.now(), None, None
    return operation


class OperationRunner:
    def __init__(self, database, settings, transport, models):
        self.database, self.settings, self.transport, self.models = (
            database,
            settings,
            transport,
            models,
        )

    async def allowed(self, user_id):
        if user_id == self.settings.admin_telegram_id:
            return True
        async with self.database.sessions() as session:
            user = await session.get(User, user_id)
            return user is not None and user.status == "approved"

    async def claim(self):
        async with self.database.sessions.begin() as session:
            # A process may have died between sending the first status and storing its ID.
            await session.execute(
                update(Operation)
                .where(
                    Operation.state == "initializing",
                    Operation.available_at < func.now() - timedelta(minutes=2),
                )
                .values(
                    state="failed",
                    error_type="StatusDeliveryUncertain",
                    status_send_state="uncertain",
                )
            )
            row = await session.scalar(
                select(Operation)
                .where(
                    Operation.status_message_id.is_not(None),
                    Operation.available_at <= func.now(),
                    or_(
                        Operation.state == "pending",
                        and_(Operation.state == "running", Operation.lease_until < func.now()),
                    ),
                )
                .order_by(Operation.available_at, Operation.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            row.state, row.claim_token = "running", uuid4()
            row.lease_until = func.now() + timedelta(seconds=LEASE_SECONDS)
            row.attempts += 1
            await session.flush()
            return row

    async def finish(self, row, **values):
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                update(Operation)
                .where(
                    Operation.id == row.id,
                    Operation.claim_token == row.claim_token,
                    Operation.state == "running",
                )
                .values(claim_token=None, lease_until=None, **values)
            )
            return result.rowcount == 1

    async def heartbeat(self, row):
        while True:
            await asyncio.sleep(10)
            async with self.database.sessions.begin() as session:
                result = await session.execute(
                    update(Operation)
                    .where(
                        Operation.id == row.id,
                        Operation.claim_token == row.claim_token,
                        Operation.state == "running",
                    )
                    .values(lease_until=func.now() + timedelta(seconds=LEASE_SECONDS))
                )
                if not result.rowcount:
                    return

    async def render(self, row, text):
        async with asyncio.timeout(self.settings.telegram_timeout_seconds):
            await self.transport.send(
                "edit_message_text",
                {"chat_id": row.chat_id, "message_id": row.status_message_id, "text": text},
            )

    async def process_one(self):
        row = await self.claim()
        if row is None:
            return False
        heartbeat = asyncio.create_task(self.heartbeat(row))
        try:
            if not await self.allowed(row.user_id):
                await self.render(row, "Processing stopped: access revoked.")
                await self.finish(row, state="cancelled", error_type="AccessRevoked")
                return True
            if row.attempts > 3:
                raise ValueError("RetryLimit")
            async with asyncio.timeout(self.settings.operation_timeout_seconds):
                if row.step == "transcribe":
                    await self.render(row, "Transcribing…")
                    german = await self.models.transcribe(row, self.transport)
                    if not german.strip() or len(german) > 10000:
                        raise ValueError("TranscriptionLength")
                    await self.finish(
                        row,
                        state="pending",
                        step="translate",
                        german=german.strip(),
                        attempts=0,
                        error_type=None,
                        available_at=func.now(),
                    )
                elif row.step == "translate":
                    await self.render(row, "Translating…")
                    english = await self.models.translate(row)
                    if not english.strip() or len(english) > 20000:
                        raise ValueError("TranslationLength")
                    await self.finish(
                        row,
                        state="pending",
                        step="complete",
                        english=english.strip(),
                        attempts=0,
                        error_type=None,
                        available_at=func.now(),
                    )
                else:
                    result = f"German:\n{row.german}\n\nEnglish:\n{row.english}"
                    if len(result) > 4000:
                        german = row.german[:1800] + ("…" if len(row.german) > 1800 else "")
                        english = row.english[:1800] + ("…" if len(row.english) > 1800 else "")
                        result = f"German:\n{german}\n\nEnglish:\n{english}\n\nFull text: /result {row.id}"
                    await self.render(row, result)
                    await self.finish(row, state="done", error_type=None)
        except Exception as exc:
            permanent = isinstance(exc, ValueError) or row.attempts >= 3
            state = "failed" if permanent else "pending"
            delay = getattr(exc, "seconds", min(60, 2**row.attempts))
            finished = await self.finish(
                row,
                state=state,
                error_type=type(exc).__name__[:100],
                available_at=func.now() + timedelta(seconds=delay),
            )
            logger.warning(
                "operation_failed", extra={"job_id": str(row.id), "error_type": type(exc).__name__}
            )
            if finished and permanent:
                with suppress(Exception):
                    await self.render(
                        row, f"Could not finish {row.step}. Retry with /retry {row.id}"
                    )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def serve(self):
        async def worker():
            while True:
                try:
                    worked = await self.process_one()
                except Exception as exc:
                    logger.error("operation_worker_error", extra={"error_type": type(exc).__name__})
                    worked = False
                if not worked:
                    await asyncio.sleep(self.settings.worker_poll_seconds)

        workers = [
            asyncio.create_task(worker()) for _ in range(self.settings.operation_concurrency)
        ]
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
