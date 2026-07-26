from __future__ import annotations

import asyncio
import io
import json
import mimetypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from app.core.config import settings
from app.services.line_platform import line_platform_service


DRIVE_SCOPE = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


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
    def is_configured(self) -> bool:
        return bool(
            settings.google_service_account_json.strip()
            and settings.google_drive_worklog_folder_id.strip()
        )

    def _credentials(self):
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

        if settings.google_drive_public_share:
            try:
                client.permissions().create(
                    fileId=file_id,
                    body={"type": "anyone", "role": "reader"},
                    fields="id",
                    supportsAllDrives=True,
                ).execute()
            except Exception:
                pass

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

    async def upload_line_photo(
        self,
        *,
        message_id: str,
        employee_name: str,
        happened_at: datetime,
    ) -> DriveUploadResult:
        if not self.is_configured():
            raise GoogleDriveWorklogError("Google Drive 工作相片上傳尚未完成設定")

        content, content_type = await line_platform_service.get_message_content(message_id)
        local_dt = happened_at.astimezone(ZoneInfo(settings.timezone))
        folder_name = local_dt.strftime("%Y-%m-%d")
        extension = mimetypes.guess_extension(content_type or "") or ".jpg"
        safe_name = employee_name.strip().replace("/", "-").replace("\\", "-")
        file_name = f"{safe_name}_{local_dt.strftime('%Y%m%d_%H%M%S')}{extension}"

        return await asyncio.to_thread(
            self._upload_bytes,
            file_name=file_name,
            content=content,
            content_type=content_type or "image/jpeg",
            folder_name=folder_name,
        )


google_drive_worklog_service = GoogleDriveWorklogService()
