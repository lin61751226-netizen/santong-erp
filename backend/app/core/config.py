import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _default_public_base_url() -> str:
    external_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if external_hostname:
        return f"https://{external_hostname}"
    return "http://127.0.0.1:8000"


class Settings(BaseSettings):
    app_name: str = "三通工程自動化管理系統"
    environment: str = "development"
    database_url: str = f"sqlite:///{(DATA_DIR / 'santong.db').as_posix()}"
    default_actor_code: str = "ADMIN001"
    public_base_url: str = _default_public_base_url()
    timezone: str = "Asia/Taipei"
    line_channel_secret: str = ""
    line_channel_access_token: str = ""
    google_service_account_json: str = ""
    google_drive_worklog_folder_id: str = ""
    google_drive_public_share: bool = True
    daily_push_hour: int = 7
    daily_push_minute: int = 0

    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
