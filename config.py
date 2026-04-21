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

    # Local similarity evaluator


    # Your personal info for ATS form-filling
    your_full_name: str = "YOUR_FULL_NAME"
    your_email:     str = "YOUR_EMAIL"
    your_phone:     str = "YOUR_PHONE"
    your_linkedin:  str = "YOUR_LINKEDIN"
    your_github:    str = "YOUR_GITHUB"
    your_location:  str = "YOUR_LOCATION"

    # Pipeline tuning
    batch_size:           int   = 100
    fit_score_threshold:  int   = 75
    crawl_delay_seconds:  float = 2.0
    page_timeout_ms:      int   = 15_000
    
    # Crawling anti-detection settings
    max_retries:          int   = 3
    base_retry_delay:     float = 1.0
    random_delay_min:     float = 0.5
    random_delay_max:     float = 3.0
    dynamic_concurrency:  bool  = True
    success_rate_check_interval: int = 10

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