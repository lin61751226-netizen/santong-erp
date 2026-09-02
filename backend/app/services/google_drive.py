from __future__ import annotations

import asyncio
import logging
import time
import io
import json
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from sqlmodel import Session, select

from app.core.config import settings
from app.models import Employee
from app.services.line_platform import line_platform_service


DRIVE_SCOPE = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SYSTEM_DATA_FOLDER_NAME = "_三通系統資料"
LINE_BINDINGS_FILE_NAME = "LINE綁定資料.json"
OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"

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

    def is_configured(self) -> bool:
        folder_id = settings.google_drive_worklog_folder_id.strip()
        if not folder_id or len(folder_id) < 10:
            return False
        # OAuth 2.0 或 service account 任一種認證可用即可
        return self._has_oauth() or self._has_service_account()

    def _credentials(self):
        # 優先使用 OAuth 2.0 使用者認證（解決 service account 無儲存配額問題）
        if self._has_oauth():
            return OAuthCredentials(
                token=None,
                refresh_token=settings.google_oauth_refresh_token.strip(),
                token_uri=OAUTH_TOKEN_URI,
                client_id=settings.google_oauth_client_id.strip(),
                client_secret=settings.google_oauth_client_secret.strip(),
                scopes=DRIVE_SCOPE,
            )

        # Fallback：service account（僅供備援，無法上傳檔案到個人 Drive）
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
                if attempt < MAX_UPLOAD_RETRIES:
                    logger.info(f"等待 {RETRY_DELAY_SECONDS} 秒後重試...")
                    time.sleep(RETRY_DELAY_SECONDS)

        logger.error(f"檔案上傳最終失敗（已重試 {MAX_UPLOAD_RETRIES} 次）: {file_name}")
        raise GoogleDriveWorklogError(f"檔案上傳失敗（已重試 {MAX_UPLOAD_RETRIES} 次）: {last_exception}")
    async def upload_line_photo(
        self,
        *,
        message_id: str,
        employee_name: str,
        happened_at: datetime,
    ) -> DriveUploadResult:
        logger.info(f"開始處理 LINE 相片上傳: message_id={message_id}, 員工={employee_name}")
        if not self.is_configured():
            logger.error("Google Drive 工作相片上傳尚未完成設定")
            raise GoogleDriveWorklogError("Google Drive 工作相片上傳尚未完成設定")

        content, content_type = await line_platform_service.get_message_content(message_id)
        logger.info(f"已從 LINE 取得相片內容: {len(content)} bytes, 類型: {content_type}")
        local_dt = happened_at.astimezone(ZoneInfo(settings.timezone))
        folder_name = local_dt.strftime("%Y-%m-%d")
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
google_drive_worklog_service = GoogleDriveWorklogService()

