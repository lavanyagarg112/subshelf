from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    telegram_bot_token: str
    encryption_key: str
    db_path: str

    @classmethod
    def from_env(cls, *, require_token: bool = True) -> "AppConfig":
        encryption_key = os.getenv("SUBSHELF_ENCRYPTION_KEY", "").strip()
        if not encryption_key:
            raise RuntimeError("SUBSHELF_ENCRYPTION_KEY is required")

        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if require_token and not telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

        db_path = os.getenv("SUBSHELF_DB_PATH", "./subshelf.sqlite3").strip()
        return cls(
            telegram_bot_token=telegram_bot_token,
            encryption_key=encryption_key,
            db_path=db_path,
        )
