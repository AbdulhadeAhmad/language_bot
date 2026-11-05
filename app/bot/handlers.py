from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.models import AccessChange, User


class PropagateErrors:
    async def handle(self, exception):
        raise exception


class Handlers:
    """Native handlers make short database changes; the API sends replies after commit."""

    def __init__(self, session, settings, incoming):
        self.session, self.settings, self.incoming = session, settings, incoming
        self.effects = []
        self.bot = AsyncTeleBot(
            settings.telegram_bot_token.get_secret_value(), exception_handler=PropagateErrors()
        )
        self.bot.register_message_handler(self.start, commands=["start"])
        self.bot.register_message_handler(self.admin, commands=["admin"])
        self.bot.register_callback_query_handler(self.callback, func=lambda _: True)
        self.bot.register_message_handler(
            self.other,
            func=lambda _: True,
            content_types=["text", "voice", "audio", "document", "photo", "video", "sticker"],
        )

    def emit(self, key, method, payload, audience="status", user_id=None):
        self.effects.append(
            {
                "key": key,
                "method": method,
                "payload": payload,
                "audience": audience,
                "user_id": user_id or self.incoming.actor_id,
            }
        )

    def say(self, text, key="reply", user_id=None, keyboard=None, audience="status"):
        payload = {"chat_id": user_id or self.incoming.actor_id, "text": text}
        if keyboard:
            payload["reply_markup"] = keyboard.to_dict()
        self.emit(key, "send_message", payload, audience, user_id)

    def buttons(self, user):
        keyboard = InlineKeyboardMarkup()
        choices = {
            "pending": [("Accept", "approved"), ("Reject", "rejected")],
            "approved": [("Revoke", "revoked")],
            "rejected": [("Restore", "approved")],
            "revoked": [("Restore", "approved")],
        }
        for label, status in choices[user.status]:
            keyboard.add(
                InlineKeyboardButton(
                    label, callback_data=f"access:{user.id}:{user.version}:{status}"
                )
            )
        return keyboard

    async def start(self, message):
        sender = message.from_user
        await self.session.execute(
            insert(User)
            .values(
                id=sender.id,
                status="approved" if sender.id == self.settings.admin_telegram_id else "pending",
            )
            .on_conflict_do_nothing()
        )
        user = await self.session.scalar(select(User).where(User.id == sender.id).with_for_update())
        first = user.registered_at is None
        for field in ("first_name", "last_name", "username", "language_code"):
            setattr(user, field, getattr(sender, field, None))
        user.last_seen_at = datetime.now(UTC)
        if first:
            user.registered_at = user.last_seen_at
        if sender.id == self.settings.admin_telegram_id:
            user.status = "approved"
        self.say(
            f"Your access status: {user.status}."
            + (
                " Use /admin to manage access."
                if sender.id == self.settings.admin_telegram_id
                else ""
            )
        )
        if user.approval_notice_state == "pending" and user.status == "pending":
            user.approval_notice_state = "sending"
            name = " ".join(part for part in [user.first_name, user.last_name] if part)
            self.say(
                f"Access request\nID: {user.id}\nName: {name}\nUsername: {user.username or '—'}\nLanguage: {user.language_code or '—'}",
                key="request",
                user_id=self.settings.admin_notification_channel_id,
                keyboard=self.buttons(user),
                audience="channel",
            )

    async def admin(self, message):
        if message.from_user.id != self.settings.admin_telegram_id:
            self.say("Admin access required.")
            return
        parts = (message.text or "").split()
        if len(parts) == 3 and parts[1] == "grant":
            try:
                user_id = int(parts[2])
                if not 0 < user_id < 2**63:
                    raise ValueError()
            except ValueError:
                self.say("Use /admin grant TELEGRAM_ID with a positive numeric ID.")
                return
            await self.session.execute(insert(User).values(id=user_id).on_conflict_do_nothing())
            user = await self.session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user.status != "approved":
                await self.change(user, "approved")
            self.say(f"Access approved for {user_id}.")
            return
        await self.menu()

    async def menu(self, status="all", page=0):
        counts = dict(
            (
                await self.session.execute(select(User.status, func.count()).group_by(User.status))
            ).all()
        )
        keyboard = InlineKeyboardMarkup()
        for label in ["pending", "approved", "rejected", "revoked", "all"]:
            keyboard.add(InlineKeyboardButton(label.title(), callback_data=f"list:{label}:0"))
        self.say(
            "Users: "
            + str(sum(counts.values()))
            + "\n"
            + "\n".join(
                f"{s}: {counts.get(s, 0)}" for s in ["pending", "approved", "rejected", "revoked"]
            )
            + "\nGrant access by ID: /admin grant TELEGRAM_ID",
            keyboard=keyboard,
        )
        query = select(User).order_by(User.id)
        if status != "all":
            query = query.where(User.status == status)
        users = (await self.session.scalars(query.offset(page * 5).limit(6))).all()
        for user in users[:5]:
            self.say(
                f"{user.id} | {user.first_name or ''} {user.last_name or ''} | @{user.username or '—'} | {user.status}",
                key=f"user:{user.id}",
                keyboard=None if user.id == self.settings.admin_telegram_id else self.buttons(user),
            )
        navigation = InlineKeyboardMarkup()
        if page:
            navigation.add(
                InlineKeyboardButton("Previous", callback_data=f"list:{status}:{page - 1}")
            )
        if len(users) > 5:
            navigation.add(InlineKeyboardButton("Next", callback_data=f"list:{status}:{page + 1}"))
        if navigation.keyboard:
            self.say(f"Page {page + 1}", key="page", keyboard=navigation)

    async def change(self, user, status):
        previous = user.status
        user.status, user.version = status, user.version + 1
        self.session.add(
            AccessChange(
                user_id=user.id,
                admin_id=self.settings.admin_telegram_id,
                previous_status=previous,
                new_status=status,
            )
        )
        if user.approval_message_id:
            self.emit(
                "channel_decision",
                "edit_message_text",
                {
                    "chat_id": self.settings.admin_notification_channel_id,
                    "message_id": user.approval_message_id,
                    "text": f"User {user.id}: {status}",
                    "reply_markup": InlineKeyboardMarkup().to_dict(),
                },
                "admin",
            )
        if user.registered_at:
            self.say(f"Your access status is now {status}.", key="decision", user_id=user.id)
        return user.approval_message_id

    async def callback(self, callback):
        if (callback.data or "").startswith(
            ("library:", "entry:", "archive:", "generate:", "play:")
        ):
            user = await self.session.get(User, callback.from_user.id)
            approved = callback.from_user.id == self.settings.admin_telegram_id or (
                user and user.status == "approved"
            )
            answer = "Send /start to view or request access."
            if approved and callback.message and callback.message.chat.type == "private":
                command, value = callback.data.split(":", 1)
                if command == "library":
                    answer = await self.library(value)
                elif command == "entry":
                    answer = await self.entry(value)
                elif command == "generate":
                    answer = await self.generate_audio(value)
                elif command == "play":
                    answer = await self.play_audio(value)
                else:
                    answer = await self.archive_retry(value)
            self.emit(
                "callback",
                "answer_callback_query",
                {"callback_query_id": callback.id, "text": answer},
            )
            return
        answer = "Admin access required."
        if callback.from_user.id == self.settings.admin_telegram_id:
            parts = (callback.data or "").split(":")
            answer = "Invalid or expired action."
            if len(parts) == 3 and parts[0] == "list" and callback.message.chat.type == "private":
                if (
                    parts[1] in {"all", "pending", "approved", "rejected", "revoked"}
                    and parts[2].isdigit()
                    and int(parts[2]) <= 1000000
                ):
                    await self.menu(parts[1], int(parts[2]))
                    answer = "Updated."
            elif (
                len(parts) == 4
                and parts[0] == "access"
                and parts[1].isdigit()
                and parts[2].isdigit()
                and int(parts[1]) < 2**63
            ):
                user = await self.session.scalar(
                    select(User).where(User.id == int(parts[1])).with_for_update()
                )
                transitions = {
                    "pending": {"approved", "rejected"},
                    "approved": {"revoked"},
                    "rejected": {"approved"},
                    "revoked": {"approved"},
                }
                if user and user.id != self.settings.admin_telegram_id:
                    if user.version == int(parts[2]) and parts[3] in transitions[user.status]:
                        channel_message_id = await self.change(user, parts[3])
                        answer = f"Access {user.status}."
                        if (
                            callback.message.chat.id != self.settings.admin_notification_channel_id
                            or callback.message.message_id != channel_message_id
                        ):
                            self.emit(
                                "edit",
                                "edit_message_text",
                                {
                                    "chat_id": callback.message.chat.id,
                                    "message_id": callback.message.message_id,
                                    "text": f"User {user.id}: {user.status}",
                                    "reply_markup": InlineKeyboardMarkup().to_dict(),
                                },
                                "admin",
                            )
                    else:
                        answer = f"Already handled. Current status: {user.status}."
        self.emit(
            "callback", "answer_callback_query", {"callback_query_id": callback.id, "text": answer}
        )

    async def other(self, message):
        from app.services.operations import create_operation, retry_operation

        user = await self.session.get(User, message.from_user.id)
        approved = message.from_user.id == self.settings.admin_telegram_id or (
            user and user.status == "approved"
        )
        if not approved:
            self.say("Send /start to view or request access.")
            return
        if message.from_user.id == self.settings.admin_telegram_id and user is None:
            # Only the explicitly configured administrator bypasses registration.
            await self.session.execute(
                insert(User)
                .values(id=self.settings.admin_telegram_id, status="approved")
                .on_conflict_do_nothing()
            )
        text = message.text or ""
        command = text.split(maxsplit=1)
        if command and command[0] == "/library":
            await self.library(command[1] if len(command) == 2 else "0")
            return
        if command and command[0] == "/entry":
            await self.entry(command[1] if len(command) == 2 else "")
            return
        if text.startswith("/retry "):
            from uuid import UUID

            try:
                operation = await retry_operation(
                    self.session, UUID(text.split()[1]), message.from_user.id
                )
            except (ValueError, IndexError):
                operation = None
            if operation is None:
                self.say("No failed operation with that ID belongs to you.")
            elif operation.status_message_id:
                self.say("Retrying your operation.")
            else:
                self.status_effect(operation)
            return
        if text.startswith("/result "):
            from uuid import UUID

            from app.db.models import Operation

            try:
                operation_id = UUID(text.split()[1])
            except (ValueError, IndexError):
                self.say("Use /result with your operation ID.")
                return
            operation = await self.session.get(Operation, operation_id)
            if (
                operation is None
                or operation.user_id != message.from_user.id
                or operation.state != "done"
            ):
                self.say("No completed operation with that ID belongs to you.")
                return
            result = f"German:\n{operation.german}\n\nEnglish:\n{operation.english}"
            for index in range(0, len(result), 4000):
                self.say(result[index : index + 4000], key=f"result:{index}")
            return
        if text.startswith("/status"):
            from app.db.models import Operation

            rows = (
                await self.session.scalars(
                    select(Operation)
                    .where(Operation.user_id == message.from_user.id)
                    .order_by(Operation.created_at.desc())
                    .limit(5)
                )
            ).all()
            self.say(
                "\n\n".join(
                    f"{row.id}: {row.step} / {row.state}\nRetry: /retry {row.id}" for row in rows
                )
                or "No processing operations yet."
            )
            return
        if text.startswith("/"):
            self.say(
                "Send German text or a voice recording (up to two minutes). Use /library for saved entries or /status to check processing."
            )
            return
        voice = message.voice or message.audio
        if voice and (voice.duration > 120 or (voice.file_size or 0) > 20 * 1024 * 1024):
            self.say("Please send audio up to two minutes and 20 MiB.")
            return
        if not voice and (not text.strip() or len(text) > 1000):
            self.say("Please send German text up to 1,000 characters or a voice recording.")
            return
        operation, created = await create_operation(
            self.session,
            message.from_user.id,
            message.chat.id,
            message.message_id,
            voice.file_id if voice else None,
            None if voice else text.strip(),
        )
        if created:
            self.status_effect(operation)

    async def library(self, value):
        from app.services.collections import list_sentences

        if not value.isascii() or not value.isdigit() or len(value) > 7 or int(value) > 1000000:
            self.say("Use /library or /library PAGE (starting at 0).")
            return "Invalid page."
        page = int(value)
        rows = await list_sentences(self.session, self.incoming.actor_id, page)
        if not rows:
            self.say(
                "No saved entries on this page. Send German text or a voice recording to add one."
            )
            return "No entries."
        keyboard = InlineKeyboardMarkup()
        for row in rows[:5]:
            label = " ".join(row.german.split())[:60]
            keyboard.add(InlineKeyboardButton(label, callback_data=f"entry:{row.id}"))
        if page:
            keyboard.add(InlineKeyboardButton("Previous", callback_data=f"library:{page - 1}"))
        if len(rows) > 5:
            keyboard.add(InlineKeyboardButton("Next", callback_data=f"library:{page + 1}"))
        self.say(f"Your collection · Page {page + 1}", keyboard=keyboard)
        return "Collection opened."

    async def entry(self, value):
        from uuid import UUID

        from app.services.collections import get_owned_sentence

        try:
            sentence_id = UUID(value)
        except ValueError:
            self.say("Use /entry with a saved entry ID.")
            return "Invalid entry."
        row = await get_owned_sentence(self.session, self.incoming.actor_id, sentence_id)
        if row is None:
            self.say("Entry unavailable.")
            return "Entry unavailable."
        result = f"German:\n{row.german}\n\nEnglish:\n{row.english}"
        from app.services.archive import original_recording

        original = await original_recording(self.session, self.incoming.actor_id, row.id)
        keyboard = InlineKeyboardMarkup()
        if original:
            asset, media = original
            status = "archived" if media.archive_message_id else asset.state.replace("_", " ")
            if not media.archive_message_id and not self.settings.telegram_archive_enabled:
                status = "retained; archive storage is disabled"
            result += f"\n\nOriginal recording: {status}."
            if asset.state == "archive_failed" and self.settings.telegram_archive_enabled:
                keyboard.add(
                    InlineKeyboardButton("Retry archive", callback_data=f"archive:{row.id}")
                )
            if asset.state == "ready" and media.archive_message_id:
                keyboard.add(
                    InlineKeyboardButton(
                        "Play original recording", callback_data=f"play:{asset.id}"
                    )
                )
        from app.db.models import AudioJob
        from app.services.audio import generated_audio

        generated = await generated_audio(self.session, self.incoming.actor_id, row.id)
        if generated and generated.state == "ready":
            keyboard.add(
                InlineKeyboardButton("Play German audio", callback_data=f"play:{generated.id}")
            )
        elif self.settings.telegram_tts_enabled:
            if generated:
                job = await self.session.get(AudioJob, generated.id)
                if generated.state == "failed":
                    step = "generate" if job and job.step == "synthesize" else "save"
                    result += (
                        f"\n\nCould not {step} German audio. Your text and translation are saved."
                    )
                else:
                    result += f"\n\nGerman audio: {generated.state}."
            if not generated or generated.state == "failed":
                label = "Retry German audio" if generated else "Generate German audio"
                keyboard.add(InlineKeyboardButton(label, callback_data=f"generate:{row.id}"))
        keyboard.add(InlineKeyboardButton("Refresh entry", callback_data=f"entry:{row.id}"))
        for index in range(0, len(result), 4000):
            self.say(
                result[index : index + 4000],
                key=f"entry:{index}",
                keyboard=keyboard if index == 0 else None,
            )
        return "Entry opened."

    async def generate_audio(self, value):
        from uuid import UUID

        from app.services.audio import request_audio

        if not self.settings.telegram_tts_enabled:
            return "German audio generation is not enabled yet."
        try:
            sentence_id = UUID(value)
        except ValueError:
            return "Entry unavailable."
        asset = await request_audio(
            self.session, self.settings, self.incoming.actor_id, sentence_id
        )
        if asset is None:
            return "Entry unavailable."
        if asset.state == "ready":
            return "German audio is ready. Open the saved entry to play it."
        return "German audio queued. Refresh the entry to check progress."

    async def play_audio(self, value):
        from uuid import UUID

        from app.services.audio import playback_payload

        try:
            asset_id = UUID(value)
        except ValueError:
            return "Recording unavailable."
        payload = await playback_payload(self.session, self.incoming.actor_id, asset_id)
        if payload is None:
            return "Recording unavailable."
        self.emit("playback", "copy_message", payload)
        return "Playing recording."

    async def archive_retry(self, value):
        from uuid import UUID

        from app.services.archive import retry_archive

        if not self.settings.telegram_archive_enabled:
            return "Archive storage is not enabled."
        try:
            sentence_id = UUID(value)
        except ValueError:
            return "Entry unavailable."
        if not await retry_archive(self.session, self.incoming.actor_id, sentence_id):
            return "No failed archive belongs to you for this entry."
        return "Retrying the archive. Open the entry to check progress."

    def status_effect(self, operation):
        self.say(
            "Transcribing…" if operation.step == "transcribe" else "Translating…",
            key=f"operation:{operation.id}",
        )
        self.effects[-1]["payload"]["reply_parameters"] = {
            "message_id": operation.source_message_id,
            "allow_sending_without_reply": True,
        }

    async def dispatch(self, update):
        await self.bot.process_new_updates([update])
