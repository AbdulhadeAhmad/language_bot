from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.paths import data_root


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=data_root() / ".env", extra="ignore", hide_input_in_errors=True
    )

    database_url: SecretStr
    telegram_bot_token: SecretStr
    telegram_webhook_secret: SecretStr
    admin_telegram_id: int = Field(gt=0)
    admin_notification_channel_id: int = Field(lt=0)
    telegram_media_channel_id: int | None = None
    asr_provider: str = "faster_whisper"
    asr_model: str = "base"
    translation_provider: str = "opus_mt"
    translation_model: str = "Helsinki-NLP/opus-mt-de-en"
    tts_provider: str = "piper"
    german_tts_voices: str = "de_DE-eva_k-x_low,de_DE-karlsson-low,de_DE-thorsten-medium"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    worker_poll_seconds: float = Field(default=0.5, gt=0, le=60)
    delivery_timeout_seconds: int = Field(default=20, ge=1, le=60)
    delivery_lease_seconds: int = Field(default=90, ge=75, le=600)
    delivery_max_attempts: int = Field(default=5, ge=1, le=20)

    @field_validator("database_url")
    @classmethod
    def validate_database(cls, value: SecretStr) -> SecretStr:
        if make_url(value.get_secret_value()).drivername != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use postgresql+asyncpg")
        return value

    @field_validator("telegram_bot_token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        prefix, separator, suffix = value.get_secret_value().partition(":")
        if not separator or not prefix.isdigit() or not suffix:
            raise ValueError("TELEGRAM_BOT_TOKEN must be a BotFather token")
        return value

    @field_validator("telegram_webhook_secret")
    @classmethod
    def validate_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if not 32 <= len(secret) <= 256 or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for char in secret
        ):
            raise ValueError(
                "Webhook secret must be 32–256 characters using letters, digits, _ or -"
            )
        return value
