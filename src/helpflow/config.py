from functools import lru_cache

from pydantic import EmailStr, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="HELPFLOW_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "HelpFlow API"
    app_version: str = "0.1.0"
    database_url: str
    rabbitmq_url: str
    jwt_secret: SecretStr
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    admin_email: EmailStr = "admin@example.com"
    admin_password: SecretStr = SecretStr("ChangeMe123!")
    operator_email: EmailStr = "operator@example.com"
    operator_password: SecretStr = SecretStr("ChangeMe123!")
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr = SecretStr("local-webhook-secret")
    operator_telegram_chat_id: str | None = None
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
