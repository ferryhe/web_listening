from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    data_dir: Path = Path("./data")
    db_path: Path = Path("./data/web_listening.db")
    downloads_dir: Path = Path("./data/downloads")
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    user_agent: str = "web-listening-bot/1.0"
    request_timeout: int = 30

    model_config = {"env_prefix": "WL_", "env_file": ".env"}


settings = Settings()
