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
    # 每日上下班打卡彙整，固定於台灣時間晚間發送給指定管理人員。
    attendance_summary_hour: int = 19
    attendance_summary_minute: int = 0
    # 後台登入與權限
    default_password: str = "Santong@2026"
    # 僅供一次性帳號復原使用；完成部署後應立即清空 Render 環境變數。
    reset_employee_code_once: str = ""
    login_fail_limit: int = 3
    login_lock_minutes: int = 15
    session_expire_minutes: int = 480
    session_secret_key: str = ""
    # LINE 自然語言助理。未啟用或沒有金鑰時，維持原本的指令回覆。
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    # 語音轉文字。預設 gpt-4o-mini-transcribe（較省）。更準但較貴可改 gpt-4o-transcribe。
    openai_transcribe_model: str = "gpt-4o-mini-transcribe"
    ai_assistant_enabled: bool = False
    ai_voice_enabled: bool = True

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

    @field_validator("ai_assistant_enabled", mode="before")
    @classmethod
    def coerce_ai_assistant_enabled(cls, value):
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        return False

    @field_validator("openai_model", mode="before")
    @classmethod
    def default_openai_model(cls, value):
        if value is None or not str(value).strip():
            return "gpt-4.1-mini"
        return str(value).strip()

    @field_validator("openai_transcribe_model", mode="before")
    @classmethod
    def default_openai_transcribe_model(cls, value):
        if value is None or not str(value).strip():
            return "gpt-4o-mini-transcribe"
        return str(value).strip()

    @field_validator("ai_voice_enabled", mode="before")
    @classmethod
    def coerce_ai_voice_enabled(cls, value):
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
