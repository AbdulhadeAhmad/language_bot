from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.models import AccessChange, IncomingUpdate, TelegramDelivery, User


class PropagateErrors:
    async def handle(self, exception):
        raise exception


class Handlers:
    """One dispatch per transaction: handlers enqueue effects, never perform network I/O."""

    def __init__(self, session, settings, incoming):
        self.session, self.settings, self.incoming = session, settings, incoming
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

    def queue(self, key, method, payload, audience="status", user_id=None):
        self.session.add(
            TelegramDelivery(
                update_id=self.incoming.update_id,
                action_key=key,
                actor_id=user_id or self.incoming.actor_id,
                audience=audience,
                method=method,
                payload=payload,
            )
        )

    def say(self, text, key="reply", user_id=None, keyboard=None, audience="status"):
        payload = {"chat_id": user_id or self.incoming.actor_id, "text": text}
        if keyboard:
            payload["reply_markup"] = keyboard.to_dict()
        self.queue(key, "send_message", payload, audience, user_id)

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
        if first and user.status == "pending":
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
        # A decision from /admin must also retire the original channel request.
        request = await self.session.scalar(
            select(TelegramDelivery)
            .join(IncomingUpdate, TelegramDelivery.update_id == IncomingUpdate.update_id)
            .where(
                IncomingUpdate.actor_id == user.id,
                TelegramDelivery.action_key == "request",
                TelegramDelivery.state == "sent",
            )
            .order_by(TelegramDelivery.id.desc())
            .limit(1)
        )
        if request and request.telegram_message_id:
            self.queue(
                "channel_decision",
                "edit_message_text",
                {
                    "chat_id": self.settings.admin_notification_channel_id,
                    "message_id": request.telegram_message_id,
                    "text": f"User {user.id}: {status}",
                    "reply_markup": InlineKeyboardMarkup().to_dict(),
                },
                "admin",
            )
        if user.registered_at:
            self.say(f"Your access status is now {status}.", key="decision", user_id=user.id)
        return request.telegram_message_id if request else None

    async def callback(self, callback):
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
                            self.queue(
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
        self.queue(
            "callback", "answer_callback_query", {"callback_query_id": callback.id, "text": answer}
        )

    async def other(self, message):
        user = await self.session.get(User, message.from_user.id)
        approved = message.from_user.id == self.settings.admin_telegram_id or (
            user and user.status == "approved"
        )
        self.say(
            "Sentence saving is not available yet."
            if approved
            else "Send /start to view or request access.",
            audience="protected" if approved else "status",
        )

    async def dispatch(self, update):
        await self.bot.process_new_updates([update])
