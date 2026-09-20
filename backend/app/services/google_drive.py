from __future__ import annotations

import asyncio
import logging
import time
import io
import json
import mimetypes
import os
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from sqlmodel import Session, select

from app.core.config import settings
from app.models import Employee
from app.services.line_platform import line_platform_service


DRIVE_SCOPE = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SYSTEM_DATA_FOLDER_NAME = "_三通系統資料"
LINE_BINDINGS_FILE_NAME = "LINE綁定資料.json"
DATABASE_BACKUP_FILE_NAME = "三通資料庫最新快照.sqlite3"
MANAGEMENT_DOCUMENTS_FOLDER_NAME = "管理系統文件"
OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"

# These tables are append-only or use disable/restore semantics. A lower row
# count means an older or empty database is about to overwrite newer history.
PRESERVED_DATABASE_TABLES = (
    "employee",
    "worksite",
    "adminauditlog",
    "workassignment",
    "assignmentmember",
    "notificationbatch",
    "notificationdelivery",
    "leaverequest",
    "attendanceevent",
    "workreportevent",
    "photouploadlog",
    "grouptextlog",
    "linelinksession",
    "loginlog",
    "meetingrecord",
    "financeentry",
    "masteroption",
    "forklift",
    "forkliftinspection",
    "manageddocument",
)

logger = logging.getLogger(__name__)
MAX_UPLOAD_RETRIES = 3
RETRY_DELAY_SECONDS = 2


class GoogleDriveWorklogError(Exception):
    pass


@dataclass
class DriveUploadResult:
    file_id: str
    file_name: str
    file_url: str
    folder_id: str
    date_folder_name: str
    content_type: str


class GoogleDriveWorklogService:
    def __init__(self) -> None:
        # 所有即時／排程快照依序執行，避免較舊快照晚完成而覆蓋較新資料。
        self._database_backup_lock = threading.Lock()
        # 同一服務執行期間共用 OAuth access token，避免啟動時多項 Drive
        # 作業同時以同一 refresh token 向 Google 重新換取權杖。
        self._oauth_credentials_lock = threading.Lock()
        self._oauth_credentials: OAuthCredentials | None = None

    def _has_oauth(self) -> bool:
        return bool(
            settings.google_oauth_client_id.strip()
            and settings.google_oauth_client_secret.strip()
            and settings.google_oauth_refresh_token.strip()
        )

    def _has_service_account(self) -> bool:
        raw = settings.google_service_account_json.strip()
        if not raw:
            return False
        if raw.startswith("{"):
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                return False
            return True
        return Path(raw).exists()

    @staticmethod
    def _oauth_refresh_failure_kind(exc: RefreshError) -> str:
        """Classify OAuth failures for operations logs without exposing credentials."""
        detail = " ".join(str(part).lower() for part in exc.args)
        if "invalid_client" in detail:
            return "invalid_client"
        if "invalid_grant" in detail:
            return "invalid_grant"
        if "unauthorized_client" in detail:
            return "unauthorized_client"
        if "access_denied" in detail:
            return "access_denied"
        return "unknown_refresh_error"

    def is_configured(self) -> bool:
        folder_id = settings.google_drive_worklog_folder_id.strip()
        if not folder_id or len(folder_id) < 10:
            return False
        # OAuth 2.0 或 service account 任一種認證可用即可
        return self._has_oauth() or self._has_service_account()

    def _service_account_credentials(self):
        raw = settings.google_service_account_json.strip()
        if not raw:
            raise GoogleDriveWorklogError("GOOGLE_SERVICE_ACCOUNT_JSON 尚未設定")

        if raw.startswith("{"):
            try:
                info = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise GoogleDriveWorklogError("GOOGLE_SERVICE_ACCOUNT_JSON 不是有效的 JSON") from exc
            return service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPE)

        credential_path = Path(raw)
        if not credential_path.exists():
            raise GoogleDriveWorklogError(f"找不到 Google service account 憑證：{credential_path}")
        return service_account.Credentials.from_service_account_file(str(credential_path), scopes=DRIVE_SCOPE)

    def _oauth_credentials_from_refresh_token(self) -> OAuthCredentials:
        with self._oauth_credentials_lock:
            created = False
            if self._oauth_credentials is None:
                created = True
                self._oauth_credentials = OAuthCredentials(
                    token=None,
                    refresh_token=settings.google_oauth_refresh_token.strip(),
                    token_uri=OAUTH_TOKEN_URI,
                    client_id=settings.google_oauth_client_id.strip(),
                    client_secret=settings.google_oauth_client_secret.strip(),
                    scopes=DRIVE_SCOPE,
            )
            try:
                if created or not self._oauth_credentials.valid:
                    self._oauth_credentials.refresh(GoogleAuthRequest())
                return self._oauth_credentials
            except RefreshError:
                self._oauth_credentials = None
                raise

    def _credentials(self):
        # 優先使用 OAuth 2.0；若 refresh token 已失效，使用既有 service account
        # 讀取同一個共享資料夾，避免 Render 重啟時遺失資料庫快照。
        if self._has_oauth():
            try:
                return self._oauth_credentials_from_refresh_token()
            except RefreshError as exc:
                if not self._has_service_account():
                    raise GoogleDriveWorklogError(
                        "Google Drive OAuth 授權已失效，請管理員重新授權後更新 Render 設定"
                    ) from exc
                logger.warning(
                    "Google OAuth 更新失敗（%s），改用 service account 備援認證",
                    self._oauth_refresh_failure_kind(exc),
                )
                return self._service_account_credentials()

        return self._service_account_credentials()

    def _build_client(self):
        return build("drive", "v3", credentials=self._credentials(), cache_discovery=False)

    @staticmethod
    def _escape_drive_query(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    def _find_child(
        self,
        client,
        *,
        parent_id: str,
        name: str,
        mime_type: str | None = None,
    ) -> dict | None:
        query_parts = [
            f"'{parent_id}' in parents",
            "trashed = false",
            f"name = '{self._escape_drive_query(name)}'",
        ]
        if mime_type:
            query_parts.append(f"mimeType = '{mime_type}'")
        response = (
            client.files()
            .list(
                q=" and ".join(query_parts),
                spaces="drive",
                fields="files(id,name,mimeType,modifiedTime)",
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = response.get("files", [])
        return files[0] if files else None

    def _system_data_folder_id(self, client, *, create: bool) -> str | None:
        root_folder_id = settings.google_drive_worklog_folder_id.strip()
        if not root_folder_id:
            raise GoogleDriveWorklogError("GOOGLE_DRIVE_WORKLOG_FOLDER_ID 尚未設定")

        existing = self._find_child(
            client,
            parent_id=root_folder_id,
            name=SYSTEM_DATA_FOLDER_NAME,
            mime_type=FOLDER_MIME_TYPE,
        )
        if existing:
            return existing["id"]
        if not create:
            return None

        created = (
            client.files()
            .create(
                body={
                    "name": SYSTEM_DATA_FOLDER_NAME,
                    "mimeType": FOLDER_MIME_TYPE,
                    "parents": [root_folder_id],
                },
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        return created["id"]

    def _load_line_bindings_document(self) -> dict | None:
        client = self._build_client()
        folder_id = self._system_data_folder_id(client, create=False)
        if not folder_id:
            return None
        file_item = self._find_child(
            client,
            parent_id=folder_id,
            name=LINE_BINDINGS_FILE_NAME,
        )
        if not file_item:
            return None
        try:
            content = (
                client.files()
                .get_media(fileId=file_item["id"], supportsAllDrives=True)
                .execute()
            )
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoogleDriveWorklogError("Google Drive 的 LINE 綁定備份格式錯誤") from exc

    def _save_line_bindings_document(self, bindings: list[dict[str, str]]) -> dict:
        client = self._build_client()
        folder_id = self._system_data_folder_id(client, create=True)
        existing = self._find_child(
            client,
            parent_id=folder_id,
            name=LINE_BINDINGS_FILE_NAME,
        )
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "bindings": bindings,
        }
        media = MediaIoBaseUpload(
            io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")),
            mimetype="application/json",
            resumable=False,
        )
        if existing:
            saved = (
                client.files()
                .update(
                    fileId=existing["id"],
                    media_body=media,
                    fields="id,modifiedTime",
                    supportsAllDrives=True,
                )
                .execute()
            )
        else:
            saved = (
                client.files()
                .create(
                    body={"name": LINE_BINDINGS_FILE_NAME, "parents": [folder_id]},
                    media_body=media,
                    fields="id,modifiedTime",
                    supportsAllDrives=True,
                )
                .execute()
            )
        return {
            "status": "saved",
            "file_id": saved["id"],
            "binding_count": len(bindings),
        }

    async def backup_line_bindings(self, session: Session) -> dict:
        if not self.is_configured():
            return {"status": "unconfigured", "binding_count": 0}

        bindings = [
            {
                "employee_code": employee.employee_code,
                "line_user_id": employee.line_user_id,
            }
            for employee in session.exec(select(Employee)).all()
            if employee.line_user_id
        ]
        # Never replace a valid Drive backup with an accidentally empty local database.
        if not bindings:
            return {"status": "skipped_empty", "binding_count": 0}
        return await asyncio.to_thread(self._save_line_bindings_document, bindings)

    async def restore_line_bindings(self, session: Session) -> dict:
        if not self.is_configured():
            return {"status": "unconfigured", "restored_count": 0}

        document = await asyncio.to_thread(self._load_line_bindings_document)
        if not document:
            return {"status": "not_found", "restored_count": 0}

        records = document.get("bindings")
        if not isinstance(records, list):
            raise GoogleDriveWorklogError("Google Drive 的 LINE 綁定備份缺少 bindings 清單")

        employees = session.exec(select(Employee)).all()
        employees_by_code = {employee.employee_code: employee for employee in employees}
        occupied_user_ids = {
            employee.line_user_id: employee.employee_code
            for employee in employees
            if employee.line_user_id
        }
        restored_codes: list[str] = []
        skipped_codes: list[str] = []

        for record in records:
            if not isinstance(record, dict):
                continue
            employee_code = str(record.get("employee_code") or "").strip()
            line_user_id = str(record.get("line_user_id") or "").strip()
            employee = employees_by_code.get(employee_code)
            if not employee or not line_user_id:
                skipped_codes.append(employee_code or "unknown")
                continue
            if employee.line_user_id:
                if employee.line_user_id != line_user_id:
                    skipped_codes.append(employee_code)
                continue
            occupied_by = occupied_user_ids.get(line_user_id)
            if occupied_by and occupied_by != employee_code:
                skipped_codes.append(employee_code)
                continue

            employee.line_user_id = line_user_id
            occupied_user_ids[line_user_id] = employee_code
            session.add(employee)
            restored_codes.append(employee_code)

        if restored_codes:
            session.commit()
        return {
            "status": "restored",
            "restored_count": len(restored_codes),
            "restored_employee_codes": restored_codes,
            "skipped_employee_codes": skipped_codes,
        }

    @staticmethod
    def _sqlite_path() -> Path | None:
        prefix = "sqlite:///"
        if not settings.database_url.startswith(prefix):
            return None
        return Path(settings.database_url[len(prefix):])

    def _download_database_snapshot(self, target: Path) -> bool:
        client = self._build_client()
        folder_id = self._system_data_folder_id(client, create=False)
        if not folder_id:
            return False
        file_item = self._find_child(client, parent_id=folder_id, name=DATABASE_BACKUP_FILE_NAME)
        if not file_item:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as output:
            request = client.files().get_media(fileId=file_item["id"], supportsAllDrives=True)
            downloader = MediaIoBaseDownload(output, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return target.stat().st_size > 0

    @staticmethod
    def _database_record_counts(database_path: Path) -> dict[str, int]:
        connection = sqlite3.connect(str(database_path))
        try:
            tables = {
                row[0].lower()
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            return {
                table: (
                    connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    if table in tables
                    else 0
                )
                for table in PRESERVED_DATABASE_TABLES
            }
        finally:
            connection.close()

    def _snapshot_regressions(self, source: Path, existing: Path) -> dict[str, dict[str, int]]:
        source_counts = self._database_record_counts(source)
        existing_counts = self._database_record_counts(existing)
        return {
            table: {"local": source_counts[table], "drive": existing_counts[table]}
            for table in PRESERVED_DATABASE_TABLES
            if source_counts[table] < existing_counts[table]
        }

    @staticmethod
    def _replace_database_file(snapshot: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        local_temp = target.with_name(f".{target.name}.restore")
        try:
            # Render's system temp directory and application directory can be
            # different filesystems, so os.replace(snapshot, target) raises EXDEV.
            shutil.copyfile(snapshot, local_temp)
            os.replace(local_temp, target)
        finally:
            local_temp.unlink(missing_ok=True)

    def _upload_database_snapshot(self, source: Path) -> dict:
        client = self._build_client()
        folder_id = self._system_data_folder_id(client, create=True)
        existing = self._find_child(client, parent_id=folder_id, name=DATABASE_BACKUP_FILE_NAME)
        media = MediaIoBaseUpload(io.BytesIO(source.read_bytes()), mimetype="application/x-sqlite3", resumable=False)
        if existing:
            saved = client.files().update(
                fileId=existing["id"], media_body=media, fields="id,modifiedTime", supportsAllDrives=True,
            ).execute()
        else:
            saved = client.files().create(
                body={"name": DATABASE_BACKUP_FILE_NAME, "parents": [folder_id]},
                media_body=media, fields="id,modifiedTime", supportsAllDrives=True,
            ).execute()
        return {"status": "saved", "file_id": saved["id"], "bytes": source.stat().st_size}

    async def restore_database_snapshot(self) -> dict:
        target = self._sqlite_path()
        if target is None or not self.is_configured():
            return {"status": "skipped", "reason": "非 SQLite 或 Google Drive 未設定"}
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = Path(temp_dir) / DATABASE_BACKUP_FILE_NAME
            try:
                found = await asyncio.to_thread(self._download_database_snapshot, snapshot)
                if not found:
                    return {"status": "not_found"}
                if (
                    settings.environment != "production"
                    and target.exists()
                    and target.stat().st_size > 0
                ):
                    return {"status": "local_exists", "bytes": target.stat().st_size}
                self._replace_database_file(snapshot, target)
                return {"status": "restored", "bytes": target.stat().st_size}
            except Exception as exc:
                logger.warning("Google Drive 資料庫快照復原失敗：%s", type(exc).__name__)
                return {"status": "failed", "error": type(exc).__name__}

    def _backup_database_sync(self, source: Path) -> dict:
        with self._database_backup_lock:
            with tempfile.TemporaryDirectory() as temp_dir:
                snapshot = Path(temp_dir) / DATABASE_BACKUP_FILE_NAME
                # SQLite backup API produces a consistent snapshot while requests continue.
                source_db = sqlite3.connect(str(source))
                target_db = sqlite3.connect(str(snapshot))
                try:
                    with target_db:
                        source_db.backup(target_db)
                finally:
                    target_db.close()
                    source_db.close()
                existing_snapshot = Path(temp_dir) / f"existing-{DATABASE_BACKUP_FILE_NAME}"
                if self._download_database_snapshot(existing_snapshot):
                    regressions = self._snapshot_regressions(snapshot, existing_snapshot)
                    if regressions:
                        logger.error("拒絕以較少記錄的本機資料庫覆蓋 Google Drive：%s", regressions)
                        return {
                            "status": "skipped_regression",
                            "reason": "本機資料庫的保存記錄少於 Google Drive 快照",
                            "regressions": regressions,
                        }
                return self._upload_database_snapshot(snapshot)

    async def backup_database(self) -> dict:
        source = self._sqlite_path()
        if source is None or not source.exists() or not self.is_configured():
            return {"status": "skipped", "reason": "非 SQLite、資料庫不存在或 Google Drive 未設定"}
        try:
            return await asyncio.to_thread(self._backup_database_sync, source)
        except Exception as exc:
            logger.warning("Google Drive 資料庫快照備份失敗：%s", type(exc).__name__)
            return {"status": "failed", "error": type(exc).__name__}

    def _find_or_create_date_folder(self, folder_name: str) -> str:
        root_folder_id = settings.google_drive_worklog_folder_id.strip()
        if not root_folder_id:
            raise GoogleDriveWorklogError("GOOGLE_DRIVE_WORKLOG_FOLDER_ID 尚未設定")

        client = self._build_client()
        query = (
            f"'{root_folder_id}' in parents and trashed = false and "
            f"mimeType = '{FOLDER_MIME_TYPE}' and name = '{self._escape_drive_query(folder_name)}'"
        )
        response = (
            client.files()
            .list(q=query, spaces="drive", fields="files(id,name)", pageSize=1, supportsAllDrives=True, includeItemsFromAllDrives=True)
            .execute()
        )
        files = response.get("files", [])
        if files:
            return files[0]["id"]

        created = (
            client.files()
            .create(
                body={
                    "name": folder_name,
                    "mimeType": FOLDER_MIME_TYPE,
                    "parents": [root_folder_id],
                },
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        return created["id"]

    def _upload_bytes(
        self,
        *,
        file_name: str,
        content: bytes,
        content_type: str,
        folder_name: str,
    ) -> DriveUploadResult:
        last_exception = None
        for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
            try:
                logger.info(f"上傳嘗試 {attempt}/{MAX_UPLOAD_RETRIES}: {file_name} (大小: {len(content)} bytes)")
                client = self._build_client()
                folder_id = self._find_or_create_date_folder(folder_name)
                stream = io.BytesIO(content)
                media = MediaIoBaseUpload(stream, mimetype=content_type, resumable=False)
                created = (
                    client.files()
                    .create(
                        body={"name": file_name, "parents": [folder_id]},
                        media_body=media,
                        fields="id,webViewLink",
                        supportsAllDrives=True,
                    )
                    .execute()
                )
                file_id = created["id"]
                logger.info(f"檔案上傳成功: {file_name} (ID: {file_id})")

                if settings.google_drive_public_share:
                    try:
                        client.permissions().create(
                            fileId=file_id,
                            body={"type": "anyone", "role": "reader"},
                            fields="id",
                            supportsAllDrives=True,
                        ).execute()
                    except Exception as e:
                        logger.warning(f"設定公開分享失敗（不影響上傳）: {e}")

                metadata = (
                    client.files()
                    .get(fileId=file_id, fields="id,webViewLink", supportsAllDrives=True)
                    .execute()
                )
                file_url = metadata.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
                return DriveUploadResult(
                    file_id=file_id,
                    file_name=file_name,
                    file_url=file_url,
                    folder_id=folder_id,
                    date_folder_name=folder_name,
                    content_type=content_type,
                )
            except Exception as e:
                last_exception = e
                logger.error(f"上傳失敗（嘗試 {attempt}/{MAX_UPLOAD_RETRIES}）: {file_name}, 錯誤: {e}")
                # 授權、權限與資料夾設定不會因等待而恢復，避免同一張照片無效重試三次。
                if self._is_non_retryable_upload_error(e):
                    raise GoogleDriveWorklogError(self._friendly_upload_error(e)) from e
                if attempt < MAX_UPLOAD_RETRIES:
                    logger.info(f"等待 {RETRY_DELAY_SECONDS} 秒後重試...")
                    time.sleep(RETRY_DELAY_SECONDS)

        logger.error(f"檔案上傳最終失敗（已重試 {MAX_UPLOAD_RETRIES} 次）: {file_name}")
        raise GoogleDriveWorklogError(
            f"檔案上傳失敗（已重試 {MAX_UPLOAD_RETRIES} 次）：{self._friendly_upload_error(last_exception)}"
        )

    @staticmethod
    def _is_non_retryable_upload_error(exc: Exception) -> bool:
        if isinstance(exc, (GoogleDriveWorklogError, RefreshError)):
            return True
        if isinstance(exc, HttpError):
            return exc.resp.status in {400, 401, 403, 404}
        return False

    @staticmethod
    def _friendly_upload_error(exc: Exception | None) -> str:
        if isinstance(exc, GoogleDriveWorklogError):
            return str(exc)
        if isinstance(exc, RefreshError):
            return "Google Drive OAuth 授權已失效，請管理員重新授權"
        if isinstance(exc, HttpError):
            detail = str(exc)
            if "storageQuotaExceeded" in detail:
                return "Google Drive 上傳帳號沒有可用儲存空間，請重新授權可寫入的公司帳號"
            if exc.resp.status == 401:
                return "Google Drive 授權已失效，請管理員重新授權"
            if exc.resp.status == 403:
                return "Google Drive 資料夾沒有上傳權限，請確認授權帳號可編輯目標資料夾"
            if exc.resp.status == 404:
                return "找不到 Google Drive 目標資料夾，請確認資料夾設定"
        return str(exc) if exc else "未知錯誤"
    async def upload_line_photo(
        self,
        *,
        message_id: str,
        employee_name: str,
        happened_at: datetime,
        site_name: str | None = None,
    ) -> DriveUploadResult:
        logger.info(f"開始處理 LINE 相片上傳: message_id={message_id}, 員工={employee_name}")
        if not self.is_configured():
            logger.error("Google Drive 工作相片上傳尚未完成設定")
            raise GoogleDriveWorklogError("Google Drive 工作相片上傳尚未完成設定")

        content, content_type = await line_platform_service.get_message_content(message_id)
        logger.info(f"已從 LINE 取得相片內容: {len(content)} bytes, 類型: {content_type}")
        local_dt = happened_at.astimezone(ZoneInfo(settings.timezone))
        folder_name = local_dt.strftime("%Y-%m-%d")
        if site_name:
            safe_site = site_name.strip().replace("/", "-").replace("\\", "-")
            folder_name = f"{folder_name}_{safe_site}"
        extension = mimetypes.guess_extension(content_type or "") or ".jpg"
        safe_name = employee_name.strip().replace("/", "-").replace("\\", "-")
        file_name = f"{safe_name}_{local_dt.strftime('%Y%m%d_%H%M%S')}{extension}"
        logger.info(f"準備上傳到 Google Drive: 資料夾={folder_name}, 檔名={file_name}")

        result = await asyncio.to_thread(
            self._upload_bytes,
            file_name=file_name,
            content=content,
            content_type=content_type or "image/jpeg",
            folder_name=folder_name,
        )
        logger.info(f"LINE 相片上傳完成: {file_name} -> {result.file_url}")
        return result

    async def upload_management_document(
        self,
        *,
        file_name: str,
        content: bytes,
        content_type: str | None,
    ) -> DriveUploadResult:
        """保存後台匯入的公司檔案，與工作照片資料夾分開管理。"""
        if not self.is_configured():
            raise GoogleDriveWorklogError("Google Drive 文件庫尚未完成設定")
        safe_name = Path(file_name).name.strip().replace("/", "-").replace("\\", "-")
        if not safe_name:
            raise GoogleDriveWorklogError("文件名稱不可空白")
        timestamp = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d_%H%M%S")
        stored_name = f"{timestamp}_{safe_name}"
        return await asyncio.to_thread(
            self._upload_bytes,
            file_name=stored_name,
            content=content,
            content_type=content_type or "application/octet-stream",
            folder_name=MANAGEMENT_DOCUMENTS_FOLDER_NAME,
        )

    def _list_management_documents_sync(self) -> list[dict]:
        """讀取文件庫資料夾的原始檔中繼資料，用於部署後復原索引。"""
        client = self._build_client()
        root_folder_id = settings.google_drive_worklog_folder_id.strip()
        if not root_folder_id:
            raise GoogleDriveWorklogError("GOOGLE_DRIVE_WORKLOG_FOLDER_ID 尚未設定")
        folder = self._find_child(
            client,
            parent_id=root_folder_id,
            name=MANAGEMENT_DOCUMENTS_FOLDER_NAME,
            mime_type=FOLDER_MIME_TYPE,
        )
        if not folder:
            return []

        records: list[dict] = []
        page_token = None
        while True:
            response = (
                client.files()
                .list(
                    q=f"'{folder['id']}' in parents and trashed = false",
                    spaces="drive",
                    fields="nextPageToken,files(id,name,mimeType,size,createdTime,webViewLink)",
                    pageSize=100,
                    orderBy="createdTime desc",
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for file in response.get("files", []):
                name = str(file.get("name") or "")
                if Path(name).suffix.lower() not in {".xls", ".xlsx", ".xlsm"}:
                    continue
                records.append({
                    "drive_file_id": file["id"],
                    "drive_folder_id": folder["id"],
                    "stored_file_name": name,
                    "original_file_name": name.split("_", 2)[-1] if name.count("_") >= 2 else name,
                    "drive_url": file.get("webViewLink") or f"https://drive.google.com/file/d/{file['id']}/view",
                    "content_type": file.get("mimeType") or mimetypes.guess_type(name)[0],
                    "size_bytes": int(file.get("size") or 0),
                    "created_at": file.get("createdTime"),
                })
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return records

    async def list_management_documents(self) -> list[dict]:
        """列出已在 Google Drive 保存的公司 Excel 原始檔。"""
        if not self.is_configured():
            return []
        return await asyncio.to_thread(self._list_management_documents_sync)
google_drive_worklog_service = GoogleDriveWorklogService()

