from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded only from explicit RescueDesk variables."""

    model_config = SettingsConfigDict(
        env_prefix="RESCUEDESK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "RescueDesk"
    environment: str = "development"
    database_url: str = "sqlite:///./rescuedesk.db"
    upload_dir: Path = Path(".runtime/uploads")
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=25 * 1024 * 1024)
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"
    enable_local_ai: bool = False
    seed_demo: bool = False
    jwt_secret: str = "local-demo-only-change-before-deployment"
    jwt_expiry_minutes: int = Field(default=480, ge=5, le=1440)


@lru_cache
def get_settings() -> Settings:
    return Settings()
