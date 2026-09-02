import os
from pathlib import Path

from pydantic import field_validator
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
    # OAuth 2.0 使用者認證（優先於 service account，解決 service account 無儲存配額問題）
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_refresh_token: str = ""
    daily_push_hour: int = 7
    daily_push_minute: int = 0
    # 後台登入與權限
    default_password: str = "Santong@2026"
    login_fail_limit: int = 3
    login_lock_minutes: int = 15
    session_expire_minutes: int = 480
    session_secret_key: str = ""

    @field_validator("google_drive_public_share", mode="before")
    @classmethod
    def coerce_google_drive_public_share(cls, value):
        if isinstance(value, bool):
            return value
        if value is None:
            return True
        normalized = str(value).strip().lower()
        if normalized in {"", "1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return True

    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
