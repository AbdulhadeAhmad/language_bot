from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.models import AccessChange, InteractionState, User


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

    def say(self, text, key="reply", user_id=None, keyboard=None, audience="status", parse_mode=None):
        payload = {"chat_id": user_id or self.incoming.actor_id, "text": text}
        if keyboard:
            payload["reply_markup"] = keyboard.to_dict()
        if parse_mode:
            payload["parse_mode"] = parse_mode
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
            ("library:", "entry:", "archive:", "generate:", "generateall:", "regenerate:", "play:", "edit:", "editen:", "replace:", "deleteaudio:", "deleteentry:", "confirmdelete:", "canceldelete:", "privacy:", "discover:", "latest:", "search:", "random:", "publicentry:", "copy:", "playpublic:", "grade:")
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
                elif command == "generateall":
                    answer = await self.generate_all_audio(value)
                elif command == "regenerate":
                    answer = await self.regenerate_audio(value)
                elif command == "play":
                    answer = await self.play_audio(value)
                elif command == "playpublic":
                    answer = await self.play_audio(value)
                elif command == "edit":
                    answer = await self.begin_edit(value)
                elif command == "editen":
                    answer = await self.begin_edit(value, field="english")
                elif command == "replace":
                    answer = await self.begin_recording_replace(value)
                elif command == "deleteaudio":
                    answer = await self.remove_audio(value)
                elif command == "deleteentry":
                    answer = await self.confirm_entry_deletion(value)
                elif command == "confirmdelete":
                    answer = await self.remove_entry(value)
                elif command == "canceldelete":
                    answer = "Entry deletion cancelled."
                elif command == "privacy":
                    answer = await self.change_privacy(value)
                elif command == "discover":
                    answer = await self.discover_menu()
                elif command == "latest":
                    answer = await self.latest_public_entries(value)
                elif command == "search":
                    message_text = callback.message.text or ""
                    query = (
                        message_text.partition(": ")[2].splitlines()[0]
                        if message_text.startswith("Public search: ")
                        else ""
                    )
                    answer = await self.search_public_entries(query, value)
                elif command == "random":
                    answer = await self.random_public_entry()
                elif command == "publicentry":
                    answer = await self.public_entry(value)
                elif command == "copy":
                    answer = await self.copy_public_entry(value)
                elif command == "grade":
                    answer = await self.grade_review_question(value)
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
        if command and command[0] == "/privacy":
            await self.privacy_menu()
            return
        if command and command[0] == "/discover":
            await self.discover_menu()
            return
        if command and command[0] == "/latest":
            await self.latest_public_entries(command[1] if len(command) == 2 else "0")
            return
        if command and command[0] == "/random":
            await self.random_public_entry()
            return
        if command and command[0] == "/search":
            await self.search_public_entries(text.partition(" ")[2])
            return
        if command and command[0] == "/practice":
            if len(command) != 2:
                self.say(
                    "Choose a mode: /practice de_to_en, /practice en_to_de, "
                    "/practice dictation, or /practice listening."
                )
                return
            await self.start_practice(command[1])
            return
        if text.strip() == "/cancel":
            await self.session.execute(
                delete(InteractionState).where(InteractionState.user_id == message.from_user.id)
            )
            self.say("Edit cancelled. Your saved entry is unchanged.")
            return
        edit = await self.session.get(InteractionState, message.from_user.id, with_for_update=True)
        if edit:
            if edit.kind == "recording_replace":
                voice = message.voice or message.audio
                if not voice or voice.duration > 120 or (voice.file_size or 0) > 20 * 1024 * 1024:
                    self.say("Send an audio recording up to two minutes and 20 MiB, or /cancel.")
                    return
                from app.db.models import ArchiveJob, AudioAsset, Sentence, StoredMedia

                sentence = await self.session.scalar(
                    select(Sentence).where(
                        Sentence.id == edit.sentence_id, Sentence.user_id == message.from_user.id
                    ).with_for_update()
                )
                if sentence is None or sentence.revision != edit.sentence_revision:
                    await self.session.delete(edit)
                    self.say("Entry changed while you were replacing its recording. Start again.")
                    return
                if not self.settings.telegram_archive_enabled:
                    self.say("Recording replacement requires archive storage to be enabled.")
                    return
                staged = (await self.session.scalars(
                    select(AudioAsset).where(
                        AudioAsset.sentence_id == sentence.id,
                        AudioAsset.kind == "original",
                        AudioAsset.replacement.is_(True),
                    ).with_for_update()
                )).all()
                for prior in staged:
                    prior.replacement, prior.active = False, False
                    prior_job = await self.session.get(ArchiveJob, prior.stored_media_id, with_for_update=True)
                    if prior_job and prior_job.state in {"pending", "running"}:
                        prior_job.state, prior_job.error_type = "cancelled", "replacement_superseded"
                        prior_job.claim_token, prior_job.lease_until = None, None
                media = StoredMedia(id=uuid4(), telegram_file_id=voice.file_id)
                self.session.add(media)
                await self.session.flush()
                self.session.add(AudioAsset(
                    id=uuid4(), sentence_id=sentence.id, stored_media_id=media.id,
                    kind="original", state="archiving", active=False, replacement=True,
                ))
                self.session.add(ArchiveJob(
                    media_id=media.id,
                    source_chat_id=message.chat.id,
                    source_message_id=message.message_id,
                ))
                await self.session.delete(edit)
                self.say("Replacement received. Your current recording stays active until the new one is archived.")
                return
            if not message.text or not message.text.strip() or len(message.text.strip()) > 1000:
                self.say("Send text up to 1,000 characters, or /cancel.")
                return
            from app.db.models import Sentence, SentenceEditJob

            sentence = await self.session.scalar(
                select(Sentence).where(
                    Sentence.id == edit.sentence_id, Sentence.user_id == message.from_user.id
                ).with_for_update()
            )
            if sentence is None or sentence.revision != edit.sentence_revision:
                await self.session.delete(edit)
                self.say("Entry changed while you were editing. Your draft was discarded.")
                return
            value = message.text.strip()
            if edit.step == "english":
                sentence.english = value
                await self.session.delete(edit)
                self.say("English translation updated. German audio is unchanged.")
                await self.entry(str(sentence.id))
                return
            german = value
            job = await self.session.get(SentenceEditJob, sentence.id, with_for_update=True)
            if job is None:
                job = SentenceEditJob(
                    sentence_id=sentence.id,
                    base_revision=sentence.revision,
                    target_revision=sentence.revision + 1,
                    german=german,
                )
                self.session.add(job)
            else:
                job.base_revision = sentence.revision
                job.target_revision = sentence.revision + 1
                job.generation += 1
                job.german = german
                job.state, job.attempts, job.error_type = "pending", 0, None
                job.available_at, job.claim_token, job.lease_until = func.now(), None, None
            await self.session.delete(edit)
            self.say("Translating your edit. The saved entry stays unchanged until translation succeeds.")
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
        if row.duplicate_warning:
            result += "\n\nSimilar German text exists in your collection or a public collection."
        from app.db.models import SentenceEditJob

        edit_job = await self.session.get(SentenceEditJob, row.id)
        if edit_job and edit_job.state in {"pending", "running"}:
            result += "\n\nGerman edit: translation pending; current saved text is unchanged."
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
            keyboard.add(InlineKeyboardButton(
                "Delete original recording", callback_data=f"deleteaudio:{asset.id}"
            ))
        if self.settings.telegram_archive_enabled:
            from app.db.models import AudioAsset

            replacement = await self.session.scalar(
                select(AudioAsset).where(
                    AudioAsset.sentence_id == row.id,
                    AudioAsset.kind == "original",
                    AudioAsset.replacement.is_(True),
                )
            )
            if replacement:
                result += f"\n\nReplacement recording: {replacement.state.replace('_', ' ')}."
                if replacement.state == "archive_failed":
                    keyboard.add(
                        InlineKeyboardButton(
                            "Retry replacement archive", callback_data=f"archive:{row.id}"
                        )
                    )
            keyboard.add(
                InlineKeyboardButton("Replace original recording", callback_data=f"replace:{row.id}")
            )
        from app.db.models import Voice
        from app.services.audio import generated_audio, staged_generated_audio

        voices = (await self.session.scalars(select(Voice).order_by(Voice.id))).all()
        for voice in voices:
            generated = await generated_audio(
                self.session, self.incoming.actor_id, row.id, voice.id
            )
            staged = await staged_generated_audio(self.session, row.id, voice.id)
            voice_name = voice.id.split("-", 1)[-1].replace("-", " ").replace("_", " ").title()
            if staged:
                result += f"\n\n{voice_name} replacement audio: {staged.state}."
                if staged.state == "failed":
                    keyboard.add(InlineKeyboardButton(
                        f"Retry {voice_name} replacement", callback_data=f"regenerate:{row.id}:{voice.id}"
                    ))
            elif generated and generated.state == "ready":
                keyboard.add(InlineKeyboardButton(
                    f"Play {voice_name}", callback_data=f"play:{generated.id}"
                ))
                keyboard.add(InlineKeyboardButton(
                    f"Regenerate {voice_name}", callback_data=f"regenerate:{row.id}:{voice.id}"
                ))
            elif self.settings.telegram_tts_enabled:
                if generated and generated.state == "failed":
                    result += f"\n\nCould not generate {voice_name} audio. Your text and translation are saved."
                elif generated:
                    result += f"\n\n{voice_name} audio: {generated.state}."
                if not generated or generated.state == "failed":
                    label = f"Retry {voice_name}" if generated else f"Generate {voice_name}"
                    keyboard.add(InlineKeyboardButton(
                        label, callback_data=f"generate:{row.id}:{voice.id}"
                    ))
            if generated or staged:
                target = staged or generated
                keyboard.add(InlineKeyboardButton(
                    f"Delete {voice_name} audio", callback_data=f"deleteaudio:{target.id}"
                ))
        if self.settings.telegram_tts_enabled and voices:
            keyboard.add(InlineKeyboardButton(
                "Generate missing voices", callback_data=f"generateall:{row.id}"
            ))
        keyboard.add(InlineKeyboardButton("Refresh entry", callback_data=f"entry:{row.id}"))
        keyboard.add(InlineKeyboardButton("Edit German", callback_data=f"edit:{row.id}"))
        keyboard.add(InlineKeyboardButton("Edit English", callback_data=f"editen:{row.id}"))
        keyboard.add(InlineKeyboardButton("Delete entry", callback_data=f"deleteentry:{row.id}"))
        for index in range(0, len(result), 4000):
            self.say(
                result[index : index + 4000],
                key=f"entry:{index}",
                keyboard=keyboard if index == 0 else None,
            )
        return "Entry opened."

    async def remove_audio(self, value):
        from uuid import UUID

        from app.services.deletion import delete_audio

        try:
            asset_id = UUID(value)
        except ValueError:
            return "Audio unavailable."
        if not await delete_audio(self.session, self.incoming.actor_id, asset_id):
            return "Audio unavailable."
        self.say("Audio removed from this entry. Unshared archived media will be cleaned up.")
        return "Audio removed."

    async def confirm_entry_deletion(self, value):
        from uuid import UUID

        from app.services.collections import get_owned_sentence

        try:
            sentence_id = UUID(value)
        except ValueError:
            return "Entry unavailable."
        sentence = await get_owned_sentence(self.session, self.incoming.actor_id, sentence_id)
        if sentence is None:
            return "Entry unavailable."
        keyboard = InlineKeyboardMarkup()
        keyboard.add(
            InlineKeyboardButton("Confirm deletion", callback_data=f"confirmdelete:{sentence.id}"),
            InlineKeyboardButton("Keep entry", callback_data=f"canceldelete:{sentence.id}"),
        )
        self.say(
            "Delete this entry and its audio? Shared recordings remain available to other entries.",
            key=f"confirm-delete:{sentence.id}",
            keyboard=keyboard,
        )
        return "Confirm entry deletion."

    async def remove_entry(self, value):
        from uuid import UUID

        from app.services.deletion import delete_sentence

        try:
            sentence_id = UUID(value)
        except ValueError:
            return "Entry unavailable."
        if not await delete_sentence(self.session, self.incoming.actor_id, sentence_id):
            return "Entry unavailable."
        self.say("Entry deleted. Unshared archived recordings will be cleaned up.")
        return "Entry deleted."

    async def begin_edit(self, value, field="german"):
        from uuid import UUID

        from app.db.models import InteractionState
        from app.services.collections import get_owned_sentence

        try:
            sentence_id = UUID(value)
        except ValueError:
            return "Entry unavailable."
        sentence = await get_owned_sentence(self.session, self.incoming.actor_id, sentence_id)
        if sentence is None:
            return "Entry unavailable."
        state = await self.session.get(InteractionState, self.incoming.actor_id)
        if state is None:
            state = InteractionState(
                user_id=self.incoming.actor_id,
                kind="sentence_edit",
                sentence_id=sentence.id,
                sentence_revision=sentence.revision,
                step=field,
            )
            self.session.add(state)
        else:
            state.kind, state.sentence_id, state.sentence_revision, state.step, state.german = (
                "sentence_edit", sentence.id, sentence.revision, field, None
            )
        if field == "english":
            self.say("Send the new English translation (up to 1,000 characters). Use /cancel to stop.")
            return "English edit started."
        self.say("Send the new German sentence (up to 1,000 characters). Its English translation will be regenerated. Use /cancel to stop.")
        return "German edit started."

    async def begin_recording_replace(self, value):
        from uuid import UUID

        from app.db.models import InteractionState
        from app.services.collections import get_owned_sentence

        if not self.settings.telegram_archive_enabled:
            return "Recording replacement requires archive storage to be enabled."
        try:
            sentence_id = UUID(value)
        except ValueError:
            return "Entry unavailable."
        sentence = await get_owned_sentence(self.session, self.incoming.actor_id, sentence_id)
        if sentence is None:
            return "Entry unavailable."
        state = await self.session.get(InteractionState, self.incoming.actor_id)
        if state is None:
            state = InteractionState(
                user_id=self.incoming.actor_id,
                kind="recording_replace",
                sentence_id=sentence.id,
                sentence_revision=sentence.revision,
                step="audio",
            )
            self.session.add(state)
        else:
            state.kind, state.sentence_id, state.sentence_revision, state.step, state.german = (
                "recording_replace", sentence.id, sentence.revision, "audio", None
            )
        self.say("Send the replacement voice or audio recording (up to two minutes and 20 MiB). Use /cancel to stop.")
        return "Send replacement recording."

    async def generate_audio(self, value):
        from uuid import UUID

        from app.services.audio import request_audio

        if not self.settings.telegram_tts_enabled:
            return "German audio generation is not enabled yet."
        parts = value.split(":", 1)
        try:
            sentence_id = UUID(parts[0])
        except ValueError:
            return "Entry unavailable."
        voice_id = parts[1] if len(parts) == 2 else None
        asset = await request_audio(
            self.session, self.settings, self.incoming.actor_id, sentence_id, voice_id
        )
        if asset is None:
            return "Entry unavailable."
        if asset.state == "ready":
            return "German audio is ready. Open the saved entry to play it."
        return "German audio queued. Refresh the entry to check progress."

    async def generate_all_audio(self, value):
        from uuid import UUID

        from app.db.models import Voice
        from app.services.audio import generated_audio, request_audio

        if not self.settings.telegram_tts_enabled:
            return "German audio generation is not enabled yet."
        try:
            sentence_id = UUID(value)
        except ValueError:
            return "Entry unavailable."
        voices = (await self.session.scalars(select(Voice).order_by(Voice.id))).all()
        queued = 0
        for voice in voices:
            existing = await generated_audio(
                self.session, self.incoming.actor_id, sentence_id, voice.id
            )
            if existing is None or existing.state != "ready":
                asset = await request_audio(
                    self.session, self.settings, self.incoming.actor_id, sentence_id, voice.id
                )
                queued += bool(asset and asset.state != "ready")
        return f"Queued {queued} missing voice(s). Refresh the entry to check progress."

    async def regenerate_audio(self, value):
        from uuid import UUID

        from app.services.audio import request_audio

        if not self.settings.telegram_tts_enabled:
            return "German audio generation is not enabled yet."
        parts = value.split(":", 1)
        try:
            sentence_id = UUID(parts[0])
        except ValueError:
            return "Entry unavailable."
        if len(parts) != 2:
            return "Voice unavailable."
        asset = await request_audio(
            self.session,
            self.settings,
            self.incoming.actor_id,
            sentence_id,
            parts[1],
            regenerate=True,
        )
        if asset is None:
            return "Entry unavailable."
        return "Replacement audio queued. The current recording remains available until the new audio is ready."

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

    async def privacy_menu(self):
        user = await self.session.get(User, self.incoming.actor_id)
        if user is None or user.status != "approved":
            self.say("Approved access is required to change privacy.")
            return "Privacy unavailable."
        keyboard = InlineKeyboardMarkup()
        next_state = "public" if user.private else "private"
        label = "Make collection public" if user.private else "Make collection private"
        keyboard.add(InlineKeyboardButton(label, callback_data=f"privacy:{next_state}"))
        self.say(
            "Your collection is currently private." if user.private else "Your collection is currently public.",
            keyboard=keyboard,
        )
        return "Privacy settings opened."

    async def change_privacy(self, value):
        from app.services.discovery import set_privacy

        if value not in {"private", "public"}:
            return "Invalid privacy setting."
        private = value == "private"
        result = await set_privacy(self.session, self.incoming.actor_id, private=private)
        if result is None:
            return "Approved access is required to change privacy."
        self.say("Your collection is now private." if result else "Your collection is now public.")
        return "Privacy updated."

    async def discover_menu(self):
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("Latest public entries", callback_data="latest:0"))
        keyboard.add(InlineKeyboardButton("Random public entry", callback_data="random:0"))
        self.say("Search public entries with /search WORDS.", keyboard=keyboard)
        return "Discovery opened."

    def public_results(self, rows, title, page=None, page_command="latest"):
        if not rows:
            self.say(f"{title}\nNo public entries found.")
            return
        keyboard = InlineKeyboardMarkup()
        for row in rows[:5]:
            label = " ".join(row.german.split())[:60]
            keyboard.add(InlineKeyboardButton(label, callback_data=f"publicentry:{row.id}"))
        if page is not None:
            if page:
                keyboard.add(InlineKeyboardButton("Previous", callback_data=f"{page_command}:{page - 1}"))
            if len(rows) > 5:
                keyboard.add(InlineKeyboardButton("Next", callback_data=f"{page_command}:{page + 1}"))
        self.say(title, keyboard=keyboard)

    async def latest_public_entries(self, value="0"):
        from app.services.discovery import latest_public

        if not value.isascii() or not value.isdigit() or len(value) > 6 or int(value) > 100000:
            return "Invalid page. Use /latest PAGE, starting at 0."
        page = int(value)
        rows = await latest_public(self.session, page)
        self.public_results(rows, f"Latest public entries · Page {page + 1}", page)
        return "Latest public entries opened."

    async def search_public_entries(self, query, value="0"):
        from app.services.discovery import search_public

        if not query.strip() or len(query.strip()) > 100:
            self.say("Use /search WORDS (up to 100 characters).")
            return "Search query required."
        if not value.isascii() or not value.isdigit() or len(value) > 6 or int(value) > 100000:
            return "Invalid search page."
        page = int(value)
        rows = await search_public(self.session, query, page)
        self.public_results(rows, f"Public search: {query.strip()}", page, "search")
        return "Public search complete."

    async def random_public_entry(self):
        from app.services.discovery import random_public

        sentence = await random_public(self.session)
        if sentence is None:
            self.say("No public entries are available yet.")
            return "No public entries."
        await self.public_entry(str(sentence.id))
        return "Random public entry opened."

    async def public_entry(self, value):
        from uuid import UUID

        from app.db.models import AudioAsset, StoredMedia, Voice
        from app.services.discovery import public_sentence

        try:
            sentence_id = UUID(value)
        except ValueError:
            return "Entry unavailable."
        sentence = await public_sentence(self.session, sentence_id)
        if sentence is None:
            self.say("Entry unavailable.")
            return "Entry unavailable."
        keyboard = InlineKeyboardMarkup()
        assets = (await self.session.scalars(
            select(AudioAsset)
            .join(StoredMedia, StoredMedia.id == AudioAsset.stored_media_id)
            .where(
                AudioAsset.sentence_id == sentence.id,
                AudioAsset.active.is_(True),
                AudioAsset.state == "ready",
                StoredMedia.archive_chat_id.is_not(None),
                StoredMedia.archive_message_id.is_not(None),
                StoredMedia.delete_pending.is_(False),
            )
        )).all()
        for asset in assets:
            if asset.kind == "original":
                label = "Play original recording"
            else:
                voice = await self.session.get(Voice, asset.voice_id)
                name = voice.id.split("-", 1)[-1].replace("-", " ").replace("_", " ").title() if voice else "German voice"
                label = f"Play {name}"
            keyboard.add(InlineKeyboardButton(label, callback_data=f"playpublic:{asset.id}"))
        keyboard.add(InlineKeyboardButton("Copy to my collection", callback_data=f"copy:{sentence.id}"))
        keyboard.add(InlineKeyboardButton("Another random entry", callback_data="random:0"))
        self.say(f"German:\n{sentence.german}\n\nEnglish:\n{sentence.english}", keyboard=keyboard)
        return "Public entry opened."

    async def copy_public_entry(self, value):
        from uuid import UUID

        from app.services.discovery import copy_public_sentence

        try:
            sentence_id = UUID(value)
        except ValueError:
            return "Entry unavailable."
        copied = await copy_public_sentence(self.session, self.incoming.actor_id, sentence_id)
        if copied is None:
            return "Entry is no longer public."
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("Open copied entry", callback_data=f"entry:{copied.id}"))
        self.say(
            "Copied to your private collection."
            + (" Similar German text exists in your collection or public collections." if copied.duplicate_warning else ""),
            keyboard=keyboard,
        )
        return "Copied to your collection."

    def review_question(self, review_session, sentence):
        from html import escape

        mode = review_session.mode
        if mode == "de_to_en":
            heading, prompt, answer = "German → English", sentence.german, sentence.english
        elif mode == "en_to_de":
            heading, prompt, answer = "English → German", sentence.english, sentence.german
        else:
            return False
        keyboard = InlineKeyboardMarkup()
        for label, rating in (("Again", 1), ("Hard", 2), ("Good", 3), ("Easy", 4)):
            keyboard.add(InlineKeyboardButton(
                label,
                callback_data=f"grade:{review_session.id.hex}:{review_session.question_number}:{rating}",
            ))
        self.say(
            f"<b>{heading}</b>\n{escape(prompt)}"
            f"\n\n<span class=\"tg-spoiler\">{escape(answer)}</span>",
            keyboard=keyboard,
            parse_mode="HTML",
        )
        return True

    async def start_practice(self, mode):
        from app.services.reviews import start_session

        result, status = await start_session(self.session, self.incoming.actor_id, mode)
        if result is None:
            if status == "no_cards":
                self.say("No eligible cards are due in this mode.")
            elif status == "not_approved":
                self.say("Approved access is required to practice.")
            else:
                self.say("Use /practice followed by one of the listed modes.")
            return
        review_session, sentence = result
        self.review_question(review_session, sentence)

    async def grade_review_question(self, value):
        from uuid import UUID

        from app.services.reviews import grade_question

        parts = value.split(":")
        if len(parts) != 3 or not parts[0].isalnum() or not parts[1].isdigit() or not parts[2].isdigit():
            return "Invalid review action."
        try:
            session_id = UUID(hex=parts[0])
            question_number, rating = int(parts[1]), int(parts[2])
        except ValueError:
            return "Invalid review action."
        result, status = await grade_question(
            self.session, self.incoming.actor_id, session_id, question_number, rating
        )
        if status == "graded":
            review_session, sentence = result
            self.review_question(review_session, sentence)
            return "Grade recorded."
        if status == "complete":
            self.say("Practice complete. Great work!")
            return "Practice complete."
        return "This review question has expired."

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
