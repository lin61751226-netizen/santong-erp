from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import settings
from app.services.google_drive import GoogleDriveWorklogService


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
