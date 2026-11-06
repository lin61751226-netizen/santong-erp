from __future__ import annotations

from datetime import date
from io import BytesIO
from unittest.mock import AsyncMock, patch

import openpyxl
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import AdminAuditLog, Employee, ManagedDocument, Role, WorkHourImportLog
from app.routes import admin as admin_routes
from test_cost_month_api import _cost_workbook_bytes


class TestCostGrid:
    def setup_method(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(engine)
        self.session = Session(engine)
        self.actor = Employee(employee_code="ADMIN001", name="測試管理員", role=Role.admin, bind_token="admin")
        self.document = ManagedDocument(
            category="推高機計價", title="推高機計價", original_file_name="成本.xlsm",
            stored_file_name="成本.xlsm", drive_file_id="grid-file", drive_folder_id="folder",
            drive_url="https://drive.example/original",
            content_type="application/vnd.ms-excel.sheet.macroEnabled.12", uploaded_by_id=1,
        )
        self.session.add_all([self.actor, self.document])
        self.session.commit()
        self.content = _cost_workbook_bytes()
        self.download = AsyncMock(side_effect=lambda _: self.content)

        async def upload(**kwargs):
            self.content = kwargs["content"]
            return "https://drive.example/updated"

        self.upload = AsyncMock(side_effect=upload)
        self.backup = AsyncMock(return_value={"status": "saved"})
        self.patches = [
            patch.object(admin_routes.google_drive_worklog_service, "download_file_bytes", self.download),
            patch.object(admin_routes.google_drive_worklog_service, "update_file_bytes", self.upload),
            patch.object(admin_routes.google_drive_worklog_service, "backup_database", self.backup),
        ]
        for item in self.patches:
            item.start()
        app.dependency_overrides[get_session] = lambda: self.session
        app.dependency_overrides[get_current_actor] = lambda: self.actor
        self.client = TestClient(app)

    def teardown_method(self):
        app.dependency_overrides.clear()
        for item in self.patches:
            item.stop()
        self.session.close()

    def read(self):
        return self.client.get("/api/cost-hour-imports/grid", params={
            "document_id": self.document.id, "year": 2026, "month": 9,
        })

    def payload(self, sha):
        return {
            "document_id": self.document.id, "year": 2026, "month": 9,
            "label": "45", "expected_sha256": sha,
            "rows": [{
                "day": "2026-09-01", "normal_hours": 8, "overtime_hours": 2,
                "support_hours": None, "external_customer": "甲方", "fuel_cost": 500,
                "payer": "會計",
            }],
        }

    def test_grid_read_preview_confirm_save_and_audit(self):
        response = self.read()
        assert response.status_code == 200, response.text
        data = response.json()
        assert set(sheet["label"] for sheet in data["sheets"]) == {"45", "新竹寶山1"}
        assert data["sheets"][0]["rows"][0]["day"] == date(2026, 9, 1).isoformat()
        payload = self.payload(data["sha256"])
        preview = self.client.post("/api/cost-hour-imports/grid", json={**payload, "preview_only": True})
        assert preview.status_code == 200, preview.text
        assert preview.json()["changed_rows"] == 1
        assert preview.json()["amount_changes"]
        self.upload.assert_not_called()
        denied = self.client.post("/api/cost-hour-imports/grid", json=payload)
        assert denied.status_code == 409
        assert self.client.post("/api/cost-hour-imports/grid", json={**payload, "confirm_amount_changes": True}).json()["saved"] == 1
        workbook = openpyxl.load_workbook(BytesIO(self.content), keep_vba=True)
        sheet = workbook["11509-45"]
        assert [sheet[f"{column}5"].value for column in "BDFHIJ"] == [8, 2, None, "甲方", 500, "會計"]
        assert sheet["C5"].data_type == "f" and sheet["E5"].data_type == "f"
        workbook.close()
        assert len(self.session.exec(select(WorkHourImportLog)).all()) == 1
        audit = self.session.exec(select(AdminAuditLog).where(AdminAuditLog.action == "cost_grid_edit")).one()
        assert '"fuel_cost": 500' in audit.summary
        self.backup.assert_awaited_once()
        assert self.client.post("/api/cost-hour-imports/grid", json={**payload, "confirm_amount_changes": True}).status_code == 409

    def test_rejects_formula_and_bad_date_without_upload(self):
        sha = self.read().json()["sha256"]
        payload = self.payload(sha)
        payload["rows"][0]["external_customer"] = "=1+1"
        assert self.client.post("/api/cost-hour-imports/grid", json={**payload, "preview_only": True}).status_code == 400
        payload["rows"][0]["external_customer"] = "甲方"
        payload["rows"][0]["day"] = "2026-10-01"
        assert self.client.post("/api/cost-hour-imports/grid", json={**payload, "preview_only": True}).status_code == 400
        self.upload.assert_not_called()

    def test_edit_hours_does_not_overwrite_unedited_formula_cell(self):
        workbook = openpyxl.load_workbook(BytesIO(self.content), keep_vba=True)
        workbook["11509-45"]["H5"] = '=IF(B5="","",B5)'
        output = BytesIO()
        workbook.save(output)
        workbook.close()
        self.content = output.getvalue()
        data = self.read().json()
        payload = self.payload(data["sha256"])
        payload["rows"][0]["external_customer"] = '=IF(B5="","",B5)'
        response = self.client.post("/api/cost-hour-imports/grid", json={**payload, "confirm_amount_changes": True})
        assert response.status_code == 200, response.text
        workbook = openpyxl.load_workbook(BytesIO(self.content), keep_vba=True)
        assert workbook["11509-45"]["H5"].value == '=IF(B5="","",B5)'
        workbook.close()

    def test_worker_cannot_read_or_write_grid(self):
        worker = Employee(employee_code="D001", name="員工", role=Role.employee, bind_token="worker")
        self.session.add(worker)
        self.session.commit()
        app.dependency_overrides[get_current_actor] = lambda: worker
        assert self.read().status_code == 403
        assert self.client.post("/api/cost-hour-imports/grid", json=self.payload("a" * 64)).status_code == 403
        assert self.client.post("/api/cost-hour-imports/grid/backup").status_code == 403
        self.upload.assert_not_called()
