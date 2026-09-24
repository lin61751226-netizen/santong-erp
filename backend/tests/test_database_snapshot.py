from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from google.auth.exceptions import RefreshError
from app.core.config import settings
from app.services.google_drive import GoogleDriveWorklogError, GoogleDriveWorklogService


def _create_database(path: Path, *, attendance_rows: int = 0, login_rows: int = 0) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE attendanceevent (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE loginlog (id INTEGER PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO attendanceevent DEFAULT VALUES",
            [() for _ in range(attendance_rows)],
        )
        connection.executemany(
            "INSERT INTO loginlog DEFAULT VALUES",
            [() for _ in range(login_rows)],
        )
        connection.commit()
    finally:
        connection.close()


class DatabaseSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def test_oauth_refresh_failure_kind_hides_sensitive_detail(self) -> None:
        service = GoogleDriveWorklogService()

        result = service._oauth_refresh_failure_kind(
            RefreshError("invalid_client", {"client_secret": "must-not-log"})
        )

        self.assertEqual(result, "invalid_client")

    def test_expired_oauth_without_service_account_explains_reauthorization(self) -> None:
        service = GoogleDriveWorklogService()
        oauth_credentials = Mock()
        oauth_credentials.refresh.side_effect = RefreshError("invalid_grant")

        with (
            patch.object(settings, "google_oauth_client_id", "oauth-client"),
            patch.object(settings, "google_oauth_client_secret", "oauth-secret"),
            patch.object(settings, "google_oauth_refresh_token", "expired-token"),
            patch.object(settings, "google_service_account_json", ""),
            patch("app.services.google_drive.OAuthCredentials", return_value=oauth_credentials),
        ):
            with self.assertRaisesRegex(GoogleDriveWorklogError, "OAuth 授權已失效"):
                service._credentials()

    def test_expired_oauth_uses_service_account_fallback(self) -> None:
        service = GoogleDriveWorklogService()
        oauth_credentials = Mock()
        oauth_credentials.refresh.side_effect = RefreshError("invalid_grant")
        service_credentials = object()

        with (
            patch.object(settings, "google_oauth_client_id", "oauth-client"),
            patch.object(settings, "google_oauth_client_secret", "oauth-secret"),
            patch.object(settings, "google_oauth_refresh_token", "expired-token"),
            patch.object(settings, "google_service_account_json", "{}"),
            patch("app.services.google_drive.OAuthCredentials", return_value=oauth_credentials),
            patch(
                "app.services.google_drive.service_account.Credentials.from_service_account_info",
                return_value=service_credentials,
            ) as service_account_credentials,
        ):
            credentials = service._credentials()

        self.assertIs(credentials, service_credentials)
        oauth_credentials.refresh.assert_called_once()
        service_account_credentials.assert_called_once_with({}, scopes=["https://www.googleapis.com/auth/drive"])

    def test_expired_oauth_never_uses_service_account_for_upload(self) -> None:
        service = GoogleDriveWorklogService()
        oauth_credentials = Mock()
        oauth_credentials.refresh.side_effect = RefreshError("invalid_grant")

        with (
            patch.object(settings, "google_oauth_client_id", "oauth-client"),
            patch.object(settings, "google_oauth_client_secret", "oauth-secret"),
            patch.object(settings, "google_oauth_refresh_token", "expired-token"),
            patch.object(settings, "google_service_account_json", "{}"),
            patch("app.services.google_drive.OAuthCredentials", return_value=oauth_credentials),
            patch.object(service, "_service_account_credentials") as fallback,
        ):
            with self.assertRaisesRegex(GoogleDriveWorklogError, "OAuth 授權已失效"):
                service._credentials(for_upload=True)

        fallback.assert_not_called()

    def test_upload_without_oauth_explains_required_settings(self) -> None:
        service = GoogleDriveWorklogService()
        with (
            patch.object(settings, "google_oauth_client_id", ""),
            patch.object(settings, "google_oauth_client_secret", ""),
            patch.object(settings, "google_oauth_refresh_token", ""),
            patch.object(settings, "google_service_account_json", "{}"),
            patch.object(service, "_service_account_credentials") as fallback,
        ):
            with self.assertRaisesRegex(GoogleDriveWorklogError, "缺少可上傳的 OAuth 授權"):
                service._credentials(for_upload=True)

        fallback.assert_not_called()

    def test_oauth_refresh_is_shared_for_concurrent_drive_operations(self) -> None:
        service = GoogleDriveWorklogService()
        oauth_credentials = Mock(valid=False)

        def refresh(_request) -> None:
            oauth_credentials.valid = True

        oauth_credentials.refresh.side_effect = refresh

        with (
            patch.object(settings, "google_oauth_client_id", "oauth-client"),
            patch.object(settings, "google_oauth_client_secret", "oauth-secret"),
            patch.object(settings, "google_oauth_refresh_token", "valid-token"),
            patch("app.services.google_drive.OAuthCredentials", return_value=oauth_credentials),
        ):
            first = service._credentials()
            second = service._credentials()

        self.assertIs(first, oauth_credentials)
        self.assertIs(second, oauth_credentials)
        oauth_credentials.refresh.assert_called_once()

    async def test_management_document_upload_uses_separate_document_folder(self) -> None:
        service = GoogleDriveWorklogService()
        result = Mock()
        with (
            patch.object(service, "is_configured", return_value=True),
            patch.object(service, "_upload_bytes", return_value=result) as upload_bytes,
        ):
            uploaded = await service.upload_management_document(
                file_name="三通工程行115年推高機計價0730.xlsm",
                content=b"excel-content",
                content_type="application/vnd.ms-excel.sheet.macroEnabled.12",
            )

        self.assertIs(uploaded, result)
        self.assertEqual(upload_bytes.call_args.kwargs["folder_name"], "管理系統文件")
        self.assertTrue(upload_bytes.call_args.kwargs["file_name"].endswith("_三通工程行115年推高機計價0730.xlsm"))

    async def test_production_restore_replaces_existing_local_database(self) -> None:
        service = GoogleDriveWorklogService()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "local.sqlite3"
            drive = root / "drive.sqlite3"
            _create_database(local, attendance_rows=0)
            _create_database(drive, attendance_rows=3)

            def download(target: Path) -> bool:
                shutil.copyfile(drive, target)
                return True

            with (
                patch.object(settings, "environment", "production"),
                patch.object(settings, "database_url", f"sqlite:///{local.as_posix()}"),
                patch.object(service, "is_configured", return_value=True),
                patch.object(service, "_download_database_snapshot", side_effect=download),
            ):
                result = await service.restore_database_snapshot()

            self.assertEqual(result["status"], "restored")
            self.assertEqual(service._database_record_counts(local)["attendanceevent"], 3)
            self.assertFalse(local.with_name(f".{local.name}.restore").exists())

    def test_backup_rejects_snapshot_with_fewer_preserved_records(self) -> None:
        service = GoogleDriveWorklogService()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "local.sqlite3"
            drive = root / "drive.sqlite3"
            _create_database(local, attendance_rows=0, login_rows=2)
            _create_database(drive, attendance_rows=11, login_rows=1)

            def download(target: Path) -> bool:
                shutil.copyfile(drive, target)
                return True

            with (
                patch.object(service, "_download_database_snapshot", side_effect=download),
                patch.object(service, "_upload_database_snapshot") as upload,
            ):
                result = service._backup_database_sync(local)

            self.assertEqual(result["status"], "skipped_regression")
            self.assertEqual(
                result["regressions"]["attendanceevent"],
                {"local": 0, "drive": 11},
            )
            upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
