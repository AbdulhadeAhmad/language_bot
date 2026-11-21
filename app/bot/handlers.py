import json
from html import escape
from datetime import UTC, datetime

from app.core.idgen import find_typed_ids, format_id, generate_id, normalize_id
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from telebot.async_telebot import AsyncTeleBot
from telebot.types import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

from app.db.models import AccessChange, InteractionState, Sentence, User
from app.voice_catalog import voice_name as display_voice_name


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
        self.bot.register_inline_handler(self.inline_search, func=lambda _: True)
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
            payload["reply_markup"] = (
                keyboard.to_dict()
                if isinstance(keyboard, InlineKeyboardMarkup)
                else json.loads(keyboard.to_json())
            )
        if parse_mode:
            payload["parse_mode"] = parse_mode
        self.emit(key, "send_message", payload, audience, user_id)

    def home_keyboard(self, *, is_admin=False):
        keyboard = ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
        keyboard.row("📚 Library", "🧠 Test")
        keyboard.row("🌍 Discover", "⚙️ Settings")
        keyboard.row("📥 Activity", "🏠 Home")
        if is_admin:
            keyboard.row("🛡 Admin")
        return keyboard

    def home(self, user=None):
        is_admin = self.incoming.actor_id == self.settings.admin_telegram_id
        self.say(
            "<b>German Pal</b>\nChoose a section below, or send German text/voice to add a sentence.",
            keyboard=self.home_keyboard(is_admin=is_admin),
            parse_mode="HTML",
        )

    def library_menu(self):
        keyboard = InlineKeyboardMarkup()
        keyboard.row(
            InlineKeyboardButton("Search German", switch_inline_query_current_chat="DU:"),
            InlineKeyboardButton("Search English", switch_inline_query_current_chat="EN:"),
        )
        keyboard.add(InlineKeyboardButton("Browse saved sentences", callback_data="library:0"))
        keyboard.add(InlineKeyboardButton("Add a sentence", callback_data="addsentence:0"))
        self.say("<b>Library</b>\nSearch your saved sentences or browse the collection.", keyboard=keyboard, parse_mode="HTML")

    def test_menu(self):
        keyboard = InlineKeyboardMarkup()
        for label, mode in (
            ("German → English", "de_to_en"),
            ("English → German", "en_to_de"),
            ("Dictation", "dictation"),
            ("Listening", "listening"),
        ):
            keyboard.add(InlineKeyboardButton(label, callback_data=f"startpractice:{mode}"))
        keyboard.row(
            InlineKeyboardButton("Resume session", callback_data="resume:active"),
            InlineKeyboardButton("Stop session", callback_data="cancelpractice:active"),
        )
        self.say("<b>Test</b>\nChoose a practice mode.", keyboard=keyboard, parse_mode="HTML")

    def settings_menu(self, user):
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("Collection privacy", callback_data="openprivacy:0"))
        keyboard.add(InlineKeyboardButton("Practice limits", callback_data="limitsprompt:0"))
        keyboard.add(InlineKeyboardButton("Daily reminders", callback_data="reminderprompt:0"))
        keyboard.add(InlineKeyboardButton("Turn reminders off", callback_data="remindersoff:0"))
        keyboard.add(InlineKeyboardButton("Cancel current edit", callback_data="canceledit:0"))
        reminder = (
            f"on at {user.reminder_time.strftime('%H:%M')} ({user.reminder_timezone})"
            if user.reminder_enabled
            else "off"
        )
        privacy = "private" if user.private else "public"
        self.say(
            f"<b>Settings</b>\nCollection: {privacy}\nDaily reminders: {reminder}\n"
            f"New cards/day: {user.new_cards_per_day} · Session size: {user.review_session_limit}",
            keyboard=keyboard,
            parse_mode="HTML",
        )

    async def activity(self):
        from app.db.models import Operation

        rows = (await self.session.scalars(
            select(Operation)
            .where(Operation.user_id == self.incoming.actor_id)
            .order_by(Operation.created_at.desc())
            .limit(5)
        )).all()
        keyboard = InlineKeyboardMarkup()
        lines = []
        for row in rows:
            lines.append(f"<code>{format_id('operation', row.id)}</code> · {row.step} · {row.state}")
            if row.state in {"failed", "cancelled"}:
                keyboard.row(
                    InlineKeyboardButton("Retry", callback_data=f"retryop:{row.id}"),
                    InlineKeyboardButton("Result", callback_data=f"resultop:{row.id}"),
                )
            elif row.state == "done":
                keyboard.add(InlineKeyboardButton("View result", callback_data=f"resultop:{row.id}"))
        if rows:
            keyboard.add(InlineKeyboardButton("Refresh activity", callback_data="activity:0"))
        self.say(
            "<b>Recent activity</b>\n" + ("\n".join(lines) if lines else "No processing activity yet."),
            keyboard=keyboard if keyboard.keyboard else None,
            parse_mode="HTML",
        )

    def prompt(self, text, placeholder):
        self.say(text, keyboard=ForceReply(selective=True, input_field_placeholder=placeholder))

    async def inline_search(self, inline_query):
        query = (inline_query.query or "").strip()
        upper = query.upper()
        if upper.startswith("DU:"):
            language, term = "de", query[3:].strip()
        elif upper.startswith("EN:"):
            language, term = "en", query[3:].strip()
        else:
            language, term = None, ""

        results = []
        user = await self.session.get(User, inline_query.from_user.id)
        approved = inline_query.from_user.id == self.settings.admin_telegram_id or (
            user is not None and user.status == "approved"
        )
        if approved and language:
            from app.services.collections import search_library

            rows, next_offset = await search_library(
                self.session,
                inline_query.from_user.id,
                language,
                term,
                offset=inline_query.offset or "",
            )
            for row in rows:
                german = " ".join(row.german.split())
                english = " ".join(row.english.split())
                results.append(
                    InlineQueryResultArticle(
                        id=row.id,
                        title=german[:256],
                        description=english[:256],
                        input_message_content=InputTextMessageContent(
                            message_text=format_id("sentence", row.id)
                        ),
                    )
                )
        self.emit(
            "inline_answer",
            "answer_inline_query",
            {
                "inline_query_id": inline_query.id,
                "results": results,
                "cache_time": 0,
                "is_personal": True,
                "next_offset": next_offset if approved and language else None,
            },
        )

    async def resolve_typed_id(self, kind, body):
        if kind == "sentence":
            from app.services.collections import get_owned_sentence

            owned = await get_owned_sentence(self.session, self.incoming.actor_id, body)
            if owned is not None:
                await self.entry(body)
            else:
                await self.public_entry(body)
            return
        if kind == "user":
            user = await self.session.scalar(select(User).where(User.public_id == body))
            if user is None or (
                user.id != self.incoming.actor_id
                and self.incoming.actor_id != self.settings.admin_telegram_id
            ):
                self.say("User unavailable.")
                return
            name = " ".join(part for part in (user.first_name, user.last_name) if part) or "—"
            self.say(
                f"<b>User</b>\n{format_id('user', user.public_id)}\n"
                f"Name: {escape(name)}\nUsername: @{escape(user.username or '—')}\n"
                f"Access: {escape(user.status)}",
                parse_mode="HTML",
            )
            return
        if kind == "operation":
            await self.result_activity(body)
            return
        if kind == "audio":
            await self.play_audio(body)
            return
        self.say("That ID type cannot be opened here.")

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
                    label, callback_data=f"access:{user.public_id}:{user.version}:{status}"
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
            f"Your access status: {user.status}.\nProfile ID: {format_id('user', user.public_id)}"
            + (
                " Use /admin to manage access."
                if sender.id == self.settings.admin_telegram_id
                else ""
            ),
            keyboard=self.home_keyboard(is_admin=sender.id == self.settings.admin_telegram_id)
            if user.status == "approved"
            else None,
        )
        if user.approval_notice_state == "pending" and user.status == "pending":
            user.approval_notice_state = "sending"
            name = " ".join(part for part in [user.first_name, user.last_name] if part)
            self.say(
                f"Access request\nID: {format_id('user', user.public_id)}\nName: {name}\nUsername: {user.username or '—'}\nLanguage: {user.language_code or '—'}",
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
            await self.grant_user(parts[2])
            return
        await self.menu()

    async def grant_user(self, value):
        if self.incoming.actor_id != self.settings.admin_telegram_id:
            self.say("Admin access required.")
            return
        try:
            user_id = int(value.strip())
            if not 0 < user_id < 2**63:
                raise ValueError()
        except ValueError:
            self.say("Enter a positive numeric Telegram user ID.")
            return
        await self.session.execute(insert(User).values(id=user_id).on_conflict_do_nothing())
        user = await self.session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user.status != "approved":
            await self.change(user, "approved")
        self.say(f"Access approved for {user_id}.")

    async def menu(self, status="all", page=0):
        counts = dict(
            (
                await self.session.execute(select(User.status, func.count()).group_by(User.status))
            ).all()
        )
        keyboard = InlineKeyboardMarkup()
        for label in ["pending", "approved", "rejected", "revoked", "all"]:
            keyboard.add(InlineKeyboardButton(label.title(), callback_data=f"list:{label}:0"))
        keyboard.add(InlineKeyboardButton("Grant access by ID", callback_data="grantprompt:0"))
        self.say(
            "Users: "
            + str(sum(counts.values()))
            + "\n"
            + "\n".join(
                f"{s}: {counts.get(s, 0)}" for s in ["pending", "approved", "rejected", "revoked"]
            )
            + "\nUse the button below to approve a Telegram user ID.",
            keyboard=keyboard,
        )
        query = select(User).order_by(User.id)
        if status != "all":
            query = query.where(User.status == status)
        users = (await self.session.scalars(query.offset(page * 5).limit(6))).all()
        for user in users[:5]:
            self.say(
                f"{format_id('user', user.public_id)} | {user.first_name or ''} {user.last_name or ''} | @{user.username or '—'} | {user.status}",
                key=f"user:{user.public_id}",
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
                    "text": f"User {format_id('user', user.public_id)}: {status}",
                    "reply_markup": InlineKeyboardMarkup().to_dict(),
                },
                "admin",
            )
        if user.registered_at:
            self.say(f"Your access status is now {status}.", key="decision", user_id=user.id)
        return user.approval_message_id

    async def callback(self, callback):
        if (callback.data or "").startswith(
            ("library:", "entry:", "archive:", "generate:", "generateall:", "regenerate:", "regen:", "play:", "edit:", "editen:", "replace:", "deleteaudio:", "deleteentry:", "confirmdelete:", "canceldelete:", "privacy:", "openprivacy:", "discover:", "latest:", "search:", "searchprompt:", "random:", "publicentry:", "copy:", "playpublic:", "grade:", "startpractice:", "resume:", "cancelpractice:", "limitsprompt:", "reminderprompt:", "remindersoff:", "canceledit:", "activity:", "retryop:", "resultop:", "addsentence:", "grantprompt:")
        ):
            user = await self.session.get(User, callback.from_user.id)
            approved = callback.from_user.id == self.settings.admin_telegram_id or (
                user and user.status == "approved"
            )
            answer = "Send /start to view or request access."
            if approved and callback.message and callback.message.chat.type == "private":
                command, value = callback.data.split(":", 1)
                if command == "library":
                    answer = await self.library(value, callback.message)
                elif command == "entry":
                    answer = await self.entry(value, callback.message)
                elif command == "generate":
                    answer = await self.generate_audio(value)
                elif command == "generateall":
                    answer = await self.generate_all_audio(value)
                elif command in {"regenerate", "regen"}:
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
                elif command == "openprivacy":
                    answer = await self.privacy_menu()
                elif command == "startpractice":
                    await self.start_practice(value)
                    answer = "Practice opened."
                elif command == "resume":
                    await self.resume_practice()
                    answer = "Practice session resumed."
                elif command == "cancelpractice":
                    from app.services.reviews import cancel_session

                    cancelled = await cancel_session(self.session, callback.from_user.id)
                    answer = "Practice session cancelled." if cancelled else "No active practice session."
                    self.say(answer)
                elif command == "addsentence":
                    self.say("Send German text or a voice recording here to add it to your library.")
                    answer = "Ready for a sentence."
                elif command == "searchprompt":
                    self.prompt("🔎 Search public sentences\nReply with a word or phrase (up to 100 characters).", "Search public entries")
                    answer = "Search prompt opened."
                elif command == "limitsprompt":
                    self.prompt("🧮 Practice limits\nReply with: NEW_CARDS_PER_DAY SESSION_SIZE (for example: 20 30).", "20 30")
                    answer = "Practice limits prompt opened."
                elif command == "reminderprompt":
                    self.prompt("⏰ Daily reminder\nReply with HH:MM TIMEZONE (for example: 20:00 Europe/Berlin).", "20:00 Europe/Berlin")
                    answer = "Reminder prompt opened."
                elif command == "remindersoff":
                    user.reminder_enabled = False
                    self.say("Daily review reminders are off.")
                    answer = "Daily reminders disabled."
                elif command == "canceledit":
                    await self.session.execute(delete(InteractionState).where(InteractionState.user_id == callback.from_user.id))
                    self.say("Any pending edit or recording replacement was cancelled. The saved entry is unchanged.")
                    answer = "Pending edit cancelled."
                elif command == "activity":
                    await self.activity()
                    answer = "Activity refreshed."
                elif command == "retryop":
                    answer = await self.retry_activity(value)
                elif command == "resultop":
                    answer = await self.result_activity(value)
                elif command == "grantprompt":
                    if callback.from_user.id == self.settings.admin_telegram_id:
                        self.prompt("🛡 Grant access by Telegram ID\nReply with the user's numeric Telegram ID.", "Telegram user ID")
                        answer = "Grant access prompt opened."
                    else:
                        answer = "Admin access required."
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
                and parts[2].isdigit()
            ):
                try:
                    public_id = normalize_id(parts[1], expected_kind="user")
                except ValueError:
                    public_id = None
                user = (
                    await self.session.scalar(
                        select(User).where(User.public_id == public_id).with_for_update()
                    )
                    if public_id else None
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
                                    "text": f"User {format_id('user', user.public_id)}: {user.status}",
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
            user = await self.session.get(User, self.settings.admin_telegram_id)
        text = message.text or ""
        command = text.split(maxsplit=1)
        navigation = text.strip()
        if navigation in {
            "🏠 Home", "Library", "📚 Library", "Test", "🧠 Test", "Discover",
            "🌍 Discover", "Settings", "⚙️ Settings", "Activity", "📥 Activity", "🛡 Admin",
        }:
            if navigation == "🏠 Home":
                self.home(user)
            elif navigation in {"Library", "📚 Library"}:
                self.library_menu()
            elif navigation in {"Test", "🧠 Test"}:
                self.test_menu()
            elif navigation in {"Discover", "🌍 Discover"}:
                await self.discover_menu()
            elif navigation in {"Settings", "⚙️ Settings"}:
                self.settings_menu(user)
            elif navigation in {"Activity", "📥 Activity"}:
                await self.activity()
            else:
                await self.admin(message)
            return
        reply = getattr(message, "reply_to_message", None)
        reply_text = getattr(reply, "text", "") if reply else ""
        if reply and reply.from_user and reply.from_user.is_bot and reply_text:
            if reply_text.startswith("🔎 Search public sentences"):
                await self.search_public_entries(text)
                return
            if reply_text.startswith("🧮 Practice limits"):
                await self.review_limits(f"/reviewlimits {text}", user)
                return
            if reply_text.startswith("⏰ Daily reminder"):
                await self.reminders(f"/reminders {text}", user)
                return
            if reply_text.startswith("🛡 Grant access by Telegram ID"):
                await self.grant_user(text)
                return
        if text and not text.startswith("/"):
            import re

            marker = re.search(r"#(ST|UR|OP|AU|MD|RS|RA|MJ|AC)_", text, re.IGNORECASE)
            typed_ids = find_typed_ids(text)
            if marker and not typed_ids:
                self.say("That ID is malformed or unknown.")
                return
            if typed_ids:
                if len(typed_ids) != 1:
                    self.say("Send one typed ID at a time.")
                    return
                await self.resolve_typed_id(*typed_ids[0])
                return
        if command and command[0] == "/help":
            self.say(
                "Available commands:\n"
                "/start — register or check access\n"
                "/help — show this command list\n"
                "/library [PAGE] — browse your saved sentences\n"
                "/entry #ST_ID — open a saved sentence\n"
                "/privacy — change your collection privacy\n"
                "/discover — browse public sentences\n"
                "/latest [PAGE] — view recent public sentences\n"
                "/random — open a random public sentence\n"
                "/search WORDS — search public sentences\n"
                "/practice MODE — practice de_to_en, en_to_de, dictation, or listening\n"
                "/resume — restore your active practice question\n"
                "/reviewlimits NEW_PER_DAY SESSION_SIZE — set practice limits\n"
                "/reminders HH:MM TIMEZONE — enable daily due-card reminders\n"
                "/reminders off — disable daily reminders\n"
                "/cancelpractice — stop your active practice session\n"
                "/status — view recent processing jobs\n"
                "/retry #OP_ID — retry a failed processing job\n"
                "/result #OP_ID — show the full result of a processing job\n"
                "/cancel — cancel the current edit or recording replacement\n"
                "/admin — list access requests (administrator only)\n"
                "/admin grant TELEGRAM_ID — approve a user (administrator only)"
            )
            return
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
        if command and command[0] == "/resume":
            await self.resume_practice()
            return
        if command and command[0] == "/reviewlimits":
            await self.review_limits(text, user)
            return
        if command and command[0] == "/reminders":
            await self.reminders(text, user)
            return
        if command and command[0] == "/cancelpractice":
            from app.services.reviews import cancel_session

            cancelled = await cancel_session(self.session, message.from_user.id)
            self.say("Practice session cancelled." if cancelled else "No active practice session.")
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
                media = StoredMedia(id=generate_id(), telegram_file_id=voice.file_id)
                self.session.add(media)
                await self.session.flush()
                self.session.add(AudioAsset(
                    id=generate_id(), sentence_id=sentence.id, stored_media_id=media.id,
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
                sentence.review_revision += 1
                from app.services.reviews import reset_review_schedules

                await reset_review_schedules(
                    self.session,
                    sentence,
                    ("de_to_en", "en_to_de", "listening"),
                )
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
        if text and not text.startswith("/"):
            result = await self.submit_review_answer(text)
            if result:
                return
        if command and command[0] == "/entry":
            await self.entry(command[1] if len(command) == 2 else "")
            return
        if text.startswith("/retry "):
            try:
                operation = await retry_operation(
                    self.session, normalize_id(text.split()[1], expected_kind="operation"), message.from_user.id
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
            from app.db.models import Operation

            try:
                operation_id = normalize_id(text.split()[1], expected_kind="operation")
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
                    f"{format_id('operation', row.id)}: {row.step} / {row.state}\nRetry: /retry {format_id('operation', row.id)}" for row in rows
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

    async def library(self, value, message=None):
        from app.services.collections import list_sentences

        if not value.isascii() or not value.isdigit() or len(value) > 7 or int(value) > 1000000:
            self.say("Use /library or /library PAGE (starting at 0).")
            return "Invalid page."
        page = int(value)
        rows = await list_sentences(self.session, self.incoming.actor_id, page)
        if not rows:
            text = "No saved entries on this page. Search your library or send German text/a voice recording to add one."
            keyboard = InlineKeyboardMarkup()
            keyboard.row(
                InlineKeyboardButton("Search German", switch_inline_query_current_chat="DU:"),
                InlineKeyboardButton("Search English", switch_inline_query_current_chat="EN:"),
            )
            if message is None:
                self.say(text, keyboard=keyboard)
            else:
                self.emit(
                    "library_page",
                    "edit_message_text",
                    {
                        "chat_id": message.chat.id,
                        "message_id": message.message_id,
                        "text": text,
                        "reply_markup": keyboard.to_dict(),
                    },
                )
            return "No entries."
        keyboard = InlineKeyboardMarkup()
        keyboard.row(
            InlineKeyboardButton("Search German", switch_inline_query_current_chat="DU:"),
            InlineKeyboardButton("Search English", switch_inline_query_current_chat="EN:"),
        )
        for row in rows[:5]:
            label = " ".join(row.german.split())[:60]
            keyboard.add(InlineKeyboardButton(label, callback_data=f"entry:{row.id}"))
        if page:
            keyboard.add(InlineKeyboardButton("Previous", callback_data=f"library:{page - 1}"))
        if len(rows) > 5:
            keyboard.add(InlineKeyboardButton("Next", callback_data=f"library:{page + 1}"))
        total = await self.session.scalar(
            select(func.count())
            .select_from(Sentence)
            .where(Sentence.user_id == self.incoming.actor_id)
        )
        text = f"Your collection · Page {page + 1} of {max(1, (total + 4) // 5)}"
        if message is None:
            self.say(text, keyboard=keyboard)
        else:
            self.emit(
                "library_page",
                "edit_message_text",
                {
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                    "text": text,
                    "reply_markup": keyboard.to_dict(),
                },
            )
        return "Collection opened."

    async def entry(self, value, message=None):
        from app.services.collections import get_owned_sentence

        try:
            sentence_id = normalize_id(value, expected_kind="sentence")
        except ValueError:
            self.say("Use /entry with a saved entry ID.")
            return "Invalid entry."
        row = await get_owned_sentence(self.session, self.incoming.actor_id, sentence_id)
        if row is None:
            self.say("Entry unavailable.")
            return "Entry unavailable."
        result, keyboard = await self.build_sentence_message(row)
        if message is not None:
            self.emit(
                "entry",
                "edit_message_text",
                {
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                    "text": result,
                    "reply_markup": keyboard.to_dict(),
                },
            )
            return "Entry opened."
        self.say(result, keyboard=keyboard)
        return "Entry opened."

    async def build_sentence_message(self, row):
        """Build the detail text and inline controls for one sentence object."""
        result = (
            f"German:\n{row.german}\n\nEnglish:\n{row.english}"
            f"\n\nID: {format_id('sentence', row.id)}"
        )
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
            voice_name = display_voice_name(voice.id)
            if staged:
                result += f"\n\n{voice_name} replacement audio: {staged.state}."
                if staged.state == "failed":
                    keyboard.add(InlineKeyboardButton(
                        f"Retry {voice_name} replacement",
                        callback_data=f"regen:{row.id}:{voice.id}",
                    ))
            elif generated and generated.state == "ready":
                keyboard.add(InlineKeyboardButton(
                    f"Play {voice_name}", callback_data=f"play:{generated.id}"
                ))
                keyboard.add(InlineKeyboardButton(
                    f"Regenerate {voice_name}",
                    callback_data=f"regen:{row.id}:{voice.id}",
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
        return result, keyboard

    async def remove_audio(self, value):
        from app.services.deletion import delete_audio

        try:
            asset_id = normalize_id(value, expected_kind="audio")
        except ValueError:
            return "Audio unavailable."
        if not await delete_audio(self.session, self.incoming.actor_id, asset_id):
            return "Audio unavailable."
        self.say("Audio removed from this entry. Unshared archived media will be cleaned up.")
        return "Audio removed."

    async def confirm_entry_deletion(self, value):
        from app.services.collections import get_owned_sentence

        try:
            sentence_id = normalize_id(value, expected_kind="sentence")
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
        from app.services.deletion import delete_sentence

        try:
            sentence_id = normalize_id(value, expected_kind="sentence")
        except ValueError:
            return "Entry unavailable."
        if not await delete_sentence(self.session, self.incoming.actor_id, sentence_id):
            return "Entry unavailable."
        self.say("Entry deleted. Unshared archived recordings will be cleaned up.")
        return "Entry deleted."

    async def begin_edit(self, value, field="german"):
        from app.db.models import InteractionState
        from app.services.collections import get_owned_sentence

        try:
            sentence_id = normalize_id(value, expected_kind="sentence")
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
        from app.db.models import InteractionState
        from app.services.collections import get_owned_sentence

        if not self.settings.telegram_archive_enabled:
            return "Recording replacement requires archive storage to be enabled."
        try:
            sentence_id = normalize_id(value, expected_kind="sentence")
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
        from app.services.audio import request_audio

        if not self.settings.telegram_tts_enabled:
            return "German audio generation is not enabled yet."
        parts = value.split(":", 1)
        try:
            sentence_id = normalize_id(parts[0], expected_kind="sentence")
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
        from app.db.models import Voice
        from app.services.audio import generated_audio, request_audio

        if not self.settings.telegram_tts_enabled:
            return "German audio generation is not enabled yet."
        try:
            sentence_id = normalize_id(value, expected_kind="sentence")
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
        from app.services.audio import request_audio

        if not self.settings.telegram_tts_enabled:
            return "German audio generation is not enabled yet."
        parts = value.split(":", 1)
        try:
            sentence_id = normalize_id(parts[0], expected_kind="sentence")
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
        from app.services.audio import playback_payload

        try:
            asset_id = normalize_id(value, expected_kind="audio")
        except ValueError:
            return "Recording unavailable."
        payload = await playback_payload(self.session, self.incoming.actor_id, asset_id)
        if payload is None:
            return "Recording unavailable."
        self.emit("playback", "copy_message", payload)
        return "Playing recording."

    async def archive_retry(self, value):
        from app.services.archive import retry_archive

        if not self.settings.telegram_archive_enabled:
            return "Archive storage is not enabled."
        try:
            sentence_id = normalize_id(value, expected_kind="sentence")
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
        keyboard.add(InlineKeyboardButton("Search public entries", callback_data="searchprompt:0"))
        self.say("<b>Discover</b>\nBrowse, search, or open a random public sentence.", keyboard=keyboard, parse_mode="HTML")
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
        from app.db.models import AudioAsset, StoredMedia, Voice
        from app.services.discovery import public_sentence

        try:
            sentence_id = normalize_id(value, expected_kind="sentence")
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
                name = display_voice_name(voice.id) if voice else "German voice"
                label = f"Play {name}"
            keyboard.add(InlineKeyboardButton(label, callback_data=f"playpublic:{asset.id}"))
        keyboard.add(InlineKeyboardButton("Copy to my collection", callback_data=f"copy:{sentence.id}"))
        keyboard.add(InlineKeyboardButton("Another random entry", callback_data="random:0"))
        self.say(f"German:\n{sentence.german}\n\nEnglish:\n{sentence.english}", keyboard=keyboard)
        return "Public entry opened."

    async def copy_public_entry(self, value):
        from app.services.discovery import copy_public_sentence

        try:
            sentence_id = normalize_id(value, expected_kind="sentence")
        except ValueError:
            return "Entry unavailable."
        copied = await copy_public_sentence(
            self.session, self.incoming.actor_id, sentence_id, self.settings
        )
        if copied is None:
            return "Entry is no longer public."
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("Open copied entry", callback_data=f"entry:{copied.id}"))
        self.say(
            "Copied to your private collection."
            + (" Any missing German voice audio will be generated automatically." if self.settings.telegram_tts_enabled else "")
            + (" Similar German text exists in your collection or public collections." if copied.duplicate_warning else ""),
            keyboard=keyboard,
        )
        return "Copied to your collection."

    async def review_question(self, review_session, sentence):
        from html import escape

        mode = review_session.mode
        if mode == "de_to_en":
            heading, prompt, answer = "German → English", sentence.german, sentence.english
        elif mode == "en_to_de":
            heading, prompt, answer = "English → German", sentence.english, sentence.german
        elif mode in {"dictation", "listening"}:
            from app.services.audio import playback_payload

            audio_payload = await playback_payload(
                self.session, self.incoming.actor_id, review_session.current_audio_asset_id
            )
            if audio_payload is None:
                review_session.state = "cancelled"
                review_session.current_sentence_id = None
                review_session.current_audio_asset_id = None
                review_session.awaiting_answer = False
                review_session.completed_at = datetime.now(UTC)
                self.say("The selected audio is no longer available. Start a new session.")
                return False
            self.emit("review-audio", "copy_message", audio_payload)
            if review_session.awaiting_answer:
                prompt = (
                    "Type the German sentence you heard."
                    if mode == "dictation"
                    else "Type the English meaning of the German audio."
                )
                self.say(prompt)
                return True
            heading = "Dictation answer" if mode == "dictation" else "Listening meaning"
            prompt = f"Your answer: {review_session.submitted_answer or ''}"
            answer = sentence.german if mode == "dictation" else sentence.english
        else:
            return False
        keyboard = InlineKeyboardMarkup()
        for label, rating in (("Again", 1), ("Hard", 2), ("Good", 3), ("Easy", 4)):
            keyboard.add(InlineKeyboardButton(
                label,
                callback_data=f"grade:{review_session.id}:{review_session.question_number}:{rating}",
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
                self.say(
                    "No eligible cards are due. Dictation and listening need at least one saved entry with generated audio."
                )
            elif status == "not_approved":
                self.say("Approved access is required to practice.")
            elif status == "pending_edit":
                self.say("Finish or cancel your current edit before starting practice.")
            else:
                self.say("Use /practice followed by one of the listed modes.")
            return
        review_session, sentence = result
        await self.review_question(review_session, sentence)

    async def resume_practice(self):
        from app.db.models import ReviewSession
        from app.services.reviews import start_session

        active = await self.session.scalar(
            select(ReviewSession)
            .where(
                ReviewSession.user_id == self.incoming.actor_id,
                ReviewSession.state == "active",
            )
            .order_by(ReviewSession.started_at.desc())
        )
        if active is None:
            self.say("You have no active practice session to resume.")
            return
        result, _status = await start_session(
            self.session, self.incoming.actor_id, active.mode
        )
        if result is None:
            self.say("That practice question is no longer available. Start a new session with /practice MODE.")
            return
        await self.review_question(*result)

    async def review_limits(self, text, user):
        parts = text.split()
        if len(parts) == 1:
            self.say(
                f"New cards per day: {user.new_cards_per_day}. Session size: {user.review_session_limit}.\n"
                "Set both with /reviewlimits NEW_PER_DAY SESSION_SIZE (0–100 and 1–100)."
            )
            return
        if (
            len(parts) != 3
            or not all(part.isascii() and part.isdigit() for part in parts[1:])
        ):
            self.say("Use /reviewlimits NEW_PER_DAY SESSION_SIZE (0–100 and 1–100).")
            return
        new_limit, session_limit = map(int, parts[1:])
        if new_limit > 100 or not 1 <= session_limit <= 100:
            self.say("New cards per day must be 0–100; session size must be 1–100.")
            return
        user.new_cards_per_day = new_limit
        user.review_session_limit = session_limit
        self.say(f"Review limits saved: {new_limit} new per day, {session_limit} per session.")

    async def reminders(self, text, user):
        from datetime import time
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        parts = text.split()
        if len(parts) == 1:
            if user.reminder_enabled:
                self.say(
                    f"Daily review reminders are on at {user.reminder_time.strftime('%H:%M')} "
                    f"({user.reminder_timezone}). Disable them with /reminders off."
                )
            else:
                self.say(
                    "Daily review reminders are off. Enable with /reminders HH:MM TIMEZONE, "
                    "for example /reminders 20:00 Europe/Berlin."
                )
            return
        if len(parts) == 2 and parts[1].lower() == "off":
            user.reminder_enabled = False
            self.say("Daily review reminders are off.")
            return
        if (
            len(parts) != 3
            or len(parts[2]) > 64
            or len(parts[1]) != 5
            or parts[1][2] != ":"
            or not parts[1][:2].isascii()
            or not parts[1][:2].isdigit()
            or not parts[1][3:].isascii()
            or not parts[1][3:].isdigit()
        ):
            self.say(
                "Use /reminders HH:MM TIMEZONE or /reminders off. "
                "Example: /reminders 20:00 Europe/Berlin."
            )
            return
        try:
            reminder_time = time(int(parts[1][:2]), int(parts[1][3:]))
            ZoneInfo(parts[2])
        except (ValueError, ZoneInfoNotFoundError):
            self.say("Use a valid 24-hour time and IANA timezone, such as 20:00 Europe/Berlin.")
            return
        user.reminder_enabled = True
        user.reminder_time = reminder_time
        user.reminder_timezone = parts[2]
        self.say(f"Daily review reminders set for {parts[1]} ({user.reminder_timezone}).")

    async def submit_review_answer(self, answer):
        if not answer.strip():
            return False
        if len(answer.strip()) > 1000:
            self.say("Practice answers are limited to 1,000 characters.")
            return True
        from app.services.reviews import submit_typed_answer

        result = await submit_typed_answer(self.session, self.incoming.actor_id, answer)
        if result is None:
            return False
        await self.review_question(*result)
        return True

    async def grade_review_question(self, value):
        from app.services.reviews import grade_question

        parts = value.split(":")
        if len(parts) != 3 or not parts[0].isalnum() or not parts[1].isdigit() or not parts[2].isdigit():
            return "Invalid review action."
        try:
            session_id = normalize_id(parts[0], expected_kind="review_session")
            question_number, rating = int(parts[1]), int(parts[2])
        except ValueError:
            return "Invalid review action."
        result, status = await grade_question(
            self.session, self.incoming.actor_id, session_id, question_number, rating
        )
        if status == "graded":
            review_session, sentence = result
            await self.review_question(review_session, sentence)
            return "Grade recorded."
        if status == "complete":
            self.say("Practice complete. Great work!")
            return "Practice complete."
        return "This review question has expired."

    async def retry_activity(self, value):
        from app.services.operations import retry_operation

        try:
            operation = await retry_operation(self.session, normalize_id(value, expected_kind="operation"), self.incoming.actor_id)
        except ValueError:
            operation = None
        if operation is None:
            self.say("No failed activity with that ID can be retried.")
            return "Retry unavailable."
        if operation.status_message_id:
            self.say("Retrying your operation.")
        else:
            self.status_effect(operation)
        return "Retry started."

    async def result_activity(self, value):
        from app.db.models import Operation

        try:
            operation = await self.session.get(Operation, normalize_id(value, expected_kind="operation"))
        except ValueError:
            operation = None
        if (
            operation is None
            or operation.user_id != self.incoming.actor_id
            or operation.state != "done"
        ):
            self.say("No completed activity with that ID belongs to you.")
            return "Result unavailable."
        content = f"German:\n{operation.german or ''}\n\nEnglish:\n{operation.english or ''}"
        for index in range(0, len(content), 4000):
            self.say(content[index : index + 4000], key=f"activity_result:{index}")
        return "Result opened."

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
