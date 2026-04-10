"""
config.py
=========
Central config — loads from .env via pydantic-settings.
Import anywhere: from config import settings
"""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str = "YOUR_TELEGRAM_BOT_TOKEN"
    telegram_chat_id:   str = "YOUR_TELEGRAM_CHAT_ID"

    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_model:    str = "llama3.1:8b"
    llm_backend:   str = "ollama"

    groq_api_key:  str = "YOUR_GROQ_API_KEY"
    groq_model:    str = "llama-3.3-70b-versatile"

    # Your personal info for ATS form-filling
    your_full_name: str = "YOUR_FULL_NAME"
    your_email:     str = "YOUR_EMAIL"
    your_phone:     str = "YOUR_PHONE"
    your_linkedin:  str = "YOUR_LINKEDIN"
    your_github:    str = "YOUR_GITHUB"
    your_location:  str = "YOUR_LOCATION"

    # Pipeline tuning
    batch_size:           int   = 25
    fit_score_threshold:  int   = 75
    crawl_delay_seconds:  float = 2.0
    page_timeout_ms:      int   = 15_000

    # Paths (derived, not from .env)
    @property
    def base_dir(self) -> Path:
        return Path(__file__).parent

    @property
    def db_path(self) -> Path:
        return self.base_dir / "data" / "job_hunter.db"

    @property
    def resumes_dir(self) -> Path:
        return self.base_dir / "output" / "resumes"

    @property
    def templates_dir(self) -> Path:
        return self.base_dir / "templates"


settings = Settings()