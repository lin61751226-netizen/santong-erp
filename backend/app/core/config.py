from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    app_name: str = "三通工程自動化管理系統"
    environment: str = "development"
    database_url: str = f"sqlite:///{(DATA_DIR / 'santong.db').as_posix()}"
    default_actor_code: str = "ADMIN001"
    public_base_url: str = "http://127.0.0.1:8000"
    timezone: str = "Asia/Taipei"
    line_channel_secret: str = ""
    line_channel_access_token: str = ""
    daily_push_hour: int = 7
    daily_push_minute: int = 0

    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
