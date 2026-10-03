from __future__ import annotations

import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from app.core.config import settings
from app.services.google_drive import FOLDER_MIME_TYPE, GoogleDriveWorklogError, GoogleDriveWorklogService
from app.services.line_platform import line_platform_service


class FakeDrive:
    def __init__(self) -> None:
        self.items = {
            "legacy-folder": {"id": "legacy-folder", "name": "2026-10-03_齊裕53",
                              "parents": ["root-folder"], "mimeType": FOLDER_MIME_TYPE},
        }
        self.created = []
        self.queries = []
        self.permissions = Mock()

    def files(self):
        return self

    @staticmethod
    def request(result):
        return Mock(execute=Mock(return_value=result))

    def list(self, *, q, **kwargs):
        self.queries.append(q)
        parent = re.search(r"'((?:\\.|[^'])*)' in parents", q).group(1)
        name = re.search(r"name = '((?:\\.|[^'])*)'", q).group(1)
        parent = parent.replace("\\'", "'").replace("\\\\", "\\")
        name = name.replace("\\'", "'").replace("\\\\", "\\")
        return self.request({"files": [item for item in self.items.values()
                                       if item["parents"] == [parent] and item["name"] == name
                                       and item.get("mimeType") == FOLDER_MIME_TYPE]})

    def create(self, *, body, **kwargs):
        file_id = f"created-{len(self.created) + 1}"
        item = {"id": file_id, **body}
        self.created.append(item)
        self.items[file_id] = item
        return self.request(item)

    def get(self, *, fileId, **kwargs):
        return self.request({"id": fileId, "webViewLink": f"https://drive.google.com/file/d/{fileId}/view"})


class PhotoFolderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = GoogleDriveWorklogService()
        self.drive = FakeDrive()
        patches = [
            patch.object(settings, "google_drive_worklog_folder_id", "root-folder"),
            patch.object(settings, "google_drive_public_share", False),
            patch.object(settings, "timezone", "Asia/Taipei"),
            patch.object(self.service, "is_configured", return_value=True),
            patch.object(self.service, "_build_client", return_value=self.drive),
            patch.object(line_platform_service, "get_message_content",
                         AsyncMock(return_value=(b"photo-bytes", "image/jpeg"))),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def upload(self, site_name="齊裕53", happened_at=None):
        return await self.service.upload_line_photo(
            message_id="photo-message", employee_name="林育弘",
            happened_at=happened_at or datetime(2026, 10, 3, 1, 2, 3, tzinfo=timezone.utc),
            site_name=site_name,
        )

    async def test_upload_uses_site_month_day_and_reuses_existing_folders(self) -> None:
        first = await self.upload()
        second = await self.upload()
        folders = [item for item in self.drive.created if item.get("mimeType") == FOLDER_MIME_TYPE]
        self.assertEqual([(item["name"], item["parents"]) for item in folders], [
            ("齊裕53", ["root-folder"]), ("2026-10", [folders[0]["id"]]),
            ("2026-10-03", [folders[1]["id"]]),
        ])
        self.assertEqual(first.folder_id, folders[2]["id"])
        self.assertEqual(second.folder_id, first.folder_id)
        photos = [item for item in self.drive.created if item.get("mimeType") != FOLDER_MIME_TYPE]
        self.assertEqual([item["parents"] for item in photos], [[first.folder_id], [first.folder_id]])
        self.assertEqual(first.date_folder_name, "齊裕53/2026-10/2026-10-03")
        self.assertTrue(first.file_name.startswith("林育弘_20261003_090203"))
        self.assertEqual(self.drive.items["legacy-folder"]["parents"], ["root-folder"])
        self.drive.permissions.assert_not_called()

    async def test_same_day_at_different_sites_uses_separate_folders(self) -> None:
        first = await self.upload("齊裕53")
        second = await self.upload("善捷47")
        self.assertNotEqual(first.folder_id, second.folder_id)
        self.assertEqual(second.date_folder_name, "善捷47/2026-10/2026-10-03")
        self.assertEqual(len([item for item in self.drive.created if item.get("mimeType") == FOLDER_MIME_TYPE]), 6)

    async def test_taipei_month_and_year_boundary(self) -> None:
        previous = await self.upload(happened_at=datetime(2026, 12, 31, 15, 59, tzinfo=timezone.utc))
        following = await self.upload(happened_at=datetime(2026, 12, 31, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(previous.date_folder_name, "齊裕53/2026-12/2026-12-31")
        self.assertEqual(following.date_folder_name, "齊裕53/2027-01/2027-01-01")
        self.assertNotEqual(previous.folder_id, following.folder_id)
        self.assertEqual(len([item for item in self.drive.created if item["name"] == "齊裕53"]), 1)

    async def test_unknown_site_is_explicitly_unclassified(self) -> None:
        for site in (None, "", "   "):
            with self.subTest(site=site):
                result = await self.upload(site)
                self.assertEqual(result.date_folder_name, "未分類工地/2026-10/2026-10-03")
        self.assertEqual(len([item for item in self.drive.created if item.get("mimeType") == FOLDER_MIME_TYPE]), 3)

    async def test_site_slashes_do_not_add_unexpected_levels(self) -> None:
        result = await self.upload("  A/B\\C  ")
        self.assertEqual(result.date_folder_name, "A-B-C/2026-10/2026-10-03")
        self.assertEqual(self.drive.created[0]["name"], "A-B-C")

    async def test_quotes_are_escaped_and_folder_can_be_reused(self) -> None:
        first = await self.upload("O'Brien53")
        second = await self.upload("O'Brien53")
        self.assertEqual(first.folder_id, second.folder_id)
        self.assertIn("name = 'O\\'Brien53'", self.drive.queries[0])

    async def test_folder_failure_does_not_upload_to_root_or_legacy_folder(self) -> None:
        with patch.object(self.service, "_find_or_create_child_folder",
                          side_effect=GoogleDriveWorklogError("資料夾無權限")) as create_folder:
            with self.assertRaisesRegex(GoogleDriveWorklogError, "資料夾無權限"):
                await self.upload()
        create_folder.assert_called_once()
        self.assertEqual(self.drive.created, [])

    def test_concurrent_folder_creation_reuses_one_hierarchy(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.service._find_or_create_photo_folder,
                                   ("齊裕53", "2026-10", "2026-10-03"), client=self.drive) for _ in range(2)]
            self.assertEqual(futures[0].result(), futures[1].result())
        self.assertEqual(len(self.drive.created), 3)

    async def test_document_and_business_attachment_paths_are_unchanged(self) -> None:
        document = await self.service.upload_management_document(
            file_name="計價.xlsx", content=b"document", content_type="application/octet-stream", public_share=False,
        )
        contract = await self.service.upload_business_attachment(
            kind="contract", file_name="合約.pdf", content=b"contract", content_type="application/pdf",
        )
        certificate = await self.service.upload_business_attachment(
            kind="certificate", file_name="證照.pdf", content=b"certificate", content_type="application/pdf",
        )
        self.assertEqual([document.date_folder_name, contract.date_folder_name, certificate.date_folder_name],
                         ["管理系統文件", "合約文件", "員工證照"])
        folders = [item for item in self.drive.created if item.get("mimeType") == FOLDER_MIME_TYPE]
        self.assertTrue(all(item["parents"] == ["root-folder"] for item in folders))
        self.drive.permissions.assert_not_called()


if __name__ == "__main__":
    unittest.main()
