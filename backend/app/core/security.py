"""後台登入安全模組：密碼加鹽雜湊（PBKDF2）+ 簽名 session token（HMAC）。

使用 Python 標準庫實作，避免新增第三方套件在 Render free plan 上的安裝風險。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from app.core.config import settings

_PBKDF2_ITERATIONS = 200_000
_ALGORITHM = "pbkdf2_sha256"
_TOKEN_SIGNING_SALT = "santong-session-v1"


def _secret_key() -> str:
    """取得 session 簽章密鑰：優先 SESSION_SECRET_KEY，其次 LINE_CHANNEL_SECRET，最後固定 fallback。"""
    if settings.session_secret_key:
        return settings.session_secret_key
    if settings.line_channel_secret:
        return f"line:{settings.line_channel_secret}"
    # 固定 fallback：僅供未設定任何密鑰的開發環境，正式環境應設定 SESSION_SECRET_KEY。
    return "santong-insecure-dev-secret-change-me"


def hash_password(password: str, salt: bytes | None = None) -> str:
    """以 PBKDF2-SHA256 產生加鹽密碼雜湊，輸出格式：pbkdf2_sha256$iterations$salt_hex$digest_hex"""
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    return f"{_ALGORITHM}${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """驗證密碼。stored 為 None 或格式不符時一律回傳 False（避免時序攻擊與崩潰）。"""
    if not stored:
        return False
    try:
        algorithm, iterations_str, salt_hex, digest_hex = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate.hex(), digest_hex)


def create_session_token() -> str:
    """產生隨機 session token（raw）。"""
    return secrets.token_urlsafe(32)


def sign_token(raw_token: str) -> str:
    """以 HMAC-SHA256 簽章，輸出格式：raw.signature_hex"""
    signature = hmac.new(
        _secret_key().encode("utf-8"),
        (raw_token + _TOKEN_SIGNING_SALT).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{raw_token}.{signature}"


def verify_signed_token(signed_token: str | None) -> str | None:
    """驗證簽名，成功回傳 raw token，失敗或不存在回傳 None。"""
    if not signed_token:
        return None
    if "." not in signed_token:
        return None
    raw_token, signature = signed_token.rsplit(".", 1)
    expected = hmac.new(
        _secret_key().encode("utf-8"),
        (raw_token + _TOKEN_SIGNING_SALT).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    return raw_token
