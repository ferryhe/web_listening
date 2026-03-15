from __future__ import annotations

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    data_dir: Path = Path("./data")
    db_path: Path | None = None
    downloads_dir: Path | None = None
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    user_agent: str = "web-listening-bot/1.0"
    request_timeout: int = 30

    model_config = {"env_prefix": "WL_", "env_file": ".env"}

    @model_validator(mode="after")
    def _resolve_paths(self) -> "Settings":
        if self.db_path is None:
            self.db_path = self.data_dir / "web_listening.db"
        if self.downloads_dir is None:
            self.downloads_dir = self.data_dir / "downloads"
        return self


settings = Settings()
