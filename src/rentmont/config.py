"""Environment configuration, validated once at startup."""
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator


class Settings(BaseModel):
    openai_api_key: SecretStr
    telegram_bot_token: SecretStr
    telegram_chat_id: str = Field(min_length=1)
    openai_model: str = Field(default="gpt-5.4-mini", min_length=1)
    poll_seconds: int = Field(default=600, gt=0)
    max_rent_kc: int = Field(default=13500, gt=0)
    max_rent_and_utilities_kc: int | None = Field(default=None, gt=0)
    district_id: int = Field(default=72, gt=0)
    data_dir: Path = Path("data")
    notify_existing: bool = False

    @field_validator("openai_api_key", "telegram_bot_token")
    @classmethod
    def nonempty_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Secret must not be empty")
        return value

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls.model_validate({
            name: os.environ[name.upper()]
            for name in cls.model_fields if name.upper() in os.environ
        })
