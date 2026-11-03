from telebot.types import Update


def parse_update(payload: dict) -> Update:
    if (
        not isinstance(payload, dict)
        or type(payload.get("update_id")) is not int
        or payload["update_id"] < 0
    ):
        raise ValueError("Invalid update ID")
    return Update.de_json(payload)


def actor(update: Update, settings) -> tuple[int, int] | None:
    callback = update.callback_query
    message = callback.message if callback else update.message
    sender = callback.from_user if callback else (message.from_user if message else None)
    if not sender or sender.is_bot or not message:
        return None
    if message.chat.type == "private" and message.chat.id == sender.id:
        return sender.id, message.chat.id
    if callback and message.chat.id == settings.admin_notification_channel_id:
        return sender.id, message.chat.id
    return None
