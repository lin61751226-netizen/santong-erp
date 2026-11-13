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
from app.models import AdminAuditLog, CostGridAmountAdjustment, Employee, ManagedDocument, Role, SignSlipRecord, WorkHourImportLog, Worksite
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

    def add_sign_slip(self, slip_no="S-45-1", amount=12345):
        site = self.session.exec(select(Worksite).where(Worksite.code == "45")).first()
        if site is None:
            site = Worksite(code="45", name="測試45工地")
            self.session.add(site)
            self.session.commit()
            self.session.refresh(site)
        slip = SignSlipRecord(
            slip_no=slip_no, slip_date=date(2026, 9, 1), worksite_id=site.id, site_code="45",
            vehicles={"twoPointFive": 1}, forklift_count=1, normal_hours=4,
            overtime_hours=0, total_hours=4, amount=amount,
            vehicle_hours=[{"normal_hours": 4, "overtime_hours": 0}],
        )
        self.session.add(slip)
        self.session.commit()
        self.session.refresh(slip)
        return slip

    def test_saved_slip_becomes_pending_and_single_confirmation_saves_hours_and_signed_amount(self):
        slip = self.add_sign_slip()
        before = self.read().json()
        row = before["sheets"][0]["rows"][0]
        pending = row["pending_sign_slip"]
        assert pending["slip_id"] == slip.id
        assert pending["normal_hours"] == 4
        assert pending["signed_amount"] == 12345
        assert pending["hours_differ"] is True
        payload = {
            "document_id": self.document.id, "year": 2026, "month": 9, "label": "45",
            "expected_sha256": before["sha256"],
            "source_sign_slip_id": slip.id,
            "expected_sign_slip_updated_at": slip.updated_at.isoformat(),
            "expected_adjustment_revision": 0,
            "rows": [{"day": "2026-09-01", "normal_hours": 4, "overtime_hours": 0,
                      "support_hours": row["support_hours"], "external_customer": row["external_customer"],
                      "fuel_cost": row["fuel_cost"], "payer": row["payer"]}],
        }
        preview = self.client.post("/api/cost-hour-imports/grid", json={**payload, "preview_only": True})
        assert preview.status_code == 200, preview.text
        assert preview.json()["signed_adjustment"]["signed"] == 12345
        missing_reason = self.client.post("/api/cost-hour-imports/grid", json={**payload, "confirm_amount_changes": True})
        assert missing_reason.status_code == 422
        assert self.upload.await_count == 0
        saved = self.client.post("/api/cost-hour-imports/grid", json={**payload, "confirm_amount_changes": True,
            "sign_slip_amount_reason": "依簽認單核定"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["backup_status"] == "saved"
        after = self.read().json()["sheets"][0]["rows"][0]
        assert after["normal_hours"] == 4
        assert after["effective_total"] == 12345
        assert after["pending_sign_slip"] is None
        assert len(self.session.exec(select(WorkHourImportLog)).all()) == 1
        adjustment = self.session.exec(select(CostGridAmountAdjustment)).one()
        assert adjustment.reason == "依簽認單核定"

    def test_multiple_sign_slips_are_not_automatically_merged(self):
        first = self.add_sign_slip()
        self.add_sign_slip("S-45-2")
        before = self.read().json()
        pending = before["sheets"][0]["rows"][0]["pending_sign_slip"]
        assert pending["conflict"] is True
        payload = self.payload(before["sha256"])
        payload.update(source_sign_slip_id=first.id,
                       expected_sign_slip_updated_at=first.updated_at.isoformat())
        denied = self.client.post("/api/cost-hour-imports/grid", json=payload)
        assert denied.status_code == 409
        self.upload.assert_not_called()

    def test_source_slip_revision_must_match(self):
        slip = self.add_sign_slip()
        before = self.read().json()
        payload = self.payload(before["sha256"])
        payload.update(source_sign_slip_id=slip.id,
                       expected_sign_slip_updated_at="2026-09-01T00:00:00")
        denied = self.client.post("/api/cost-hour-imports/grid", json=payload)
        assert denied.status_code == 409
        self.upload.assert_not_called()

    def test_grid_read_preview_confirm_save_and_audit(self):
        response = self.read()
        assert response.status_code == 200, response.text
        data = response.json()
        assert set(sheet["label"] for sheet in data["sheets"]) == {"45", "新竹寶山1"}
        assert data["sheets"][0]["rows"][0]["day"] == date(2026, 9, 1).isoformat()
        for sheet in data["sheets"]:
            totals = sheet["totals"]
            assert round(totals["untaxed"], 2) == round(sum(row["total"] for row in sheet["rows"]), 2)
            assert totals["taxed"] == totals["untaxed"] + totals["tax"]
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

    def test_reviewed_amount_is_separate_from_formula_and_stales_after_formula_change(self):
        workbook = openpyxl.load_workbook(BytesIO(self.content), keep_vba=True)
        workbook["11509-45"]["B5"] = 8
        output = BytesIO()
        workbook.save(output)
        workbook.close()
        self.content = output.getvalue()
        original_content = self.content
        data = self.read().json()
        sheet = next(item for item in data["sheets"] if item["label"] == "45")
        assert sheet["rows"][0]["total"] == 6000
        payload = {"document_id": self.document.id, "day": "2026-09-01", "label": "45",
                   "expected_sha256": data["sha256"], "expected_revision": 0,
                   "manual_total_amount": 6400, "reason": "依當日簽單核定"}
        endpoint = "/api/cost-hour-imports/grid/amount-adjustment"
        preview = self.client.post(endpoint, json={**payload, "preview_only": True})
        assert preview.status_code == 200, preview.text
        assert (preview.json()["formula_amount"], preview.json()["after"]) == (6000, 6400)
        assert self.client.post(endpoint, json=payload).status_code == 409
        assert self.client.post(endpoint, json={**payload, "reason": "", "confirmed": True}).status_code == 422
        saved = self.client.post(endpoint, json={**payload, "confirmed": True})
        assert saved.status_code == 200, saved.text
        assert saved.json()["backup_status"] == "saved"
        assert self.content == original_content
        self.upload.assert_not_called()
        row = next(item for item in self.read().json()["sheets"] if item["label"] == "45")
        assert row["rows"][0]["total"] == 6000
        assert row["rows"][0]["effective_total"] == 6400
        assert row["totals"]["untaxed"] == 6400
        assert row["totals"]["tax"] == 320
        assert self.session.exec(select(CostGridAmountAdjustment)).one().revision == 1
        assert self.session.exec(select(AdminAuditLog).where(
            AdminAuditLog.action == "cost_grid_amount_adjustment")).one().actor_code == "ADMIN001"
        assert self.client.post(endpoint, json={**payload, "confirmed": True}).status_code == 409

        workbook = openpyxl.load_workbook(BytesIO(self.content), keep_vba=True)
        workbook["11509-45"]["B5"] = 4
        output = BytesIO()
        workbook.save(output)
        workbook.close()
        self.content = output.getvalue()
        changed_data = self.read().json()
        changed_row = next(item for item in changed_data["sheets"] if item["label"] == "45")["rows"][0]
        assert changed_row["adjustment_stale"] is True
        assert changed_row["effective_total"] == changed_row["total"] == 3200
        reviewed = self.client.post(endpoint, json={**payload, "expected_sha256": changed_data["sha256"],
                                                   "expected_revision": 1, "preview_only": True})
        assert reviewed.json()["changed"] is True
        cleared = self.client.post(endpoint, json={**payload, "expected_sha256": changed_data["sha256"],
                                                  "expected_revision": 1, "manual_total_amount": None,
                                                  "reason": "", "confirmed": True})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["after"] == 3200
        restored = next(item for item in self.read().json()["sheets"] if item["label"] == "45")["rows"][0]
        assert restored["effective_total"] == restored["total"] == 3200
        assert restored["adjustment_revision"] == 2

    def test_amount_adjustment_requires_admin_and_current_file(self):
        data = self.read().json()
        payload = {"document_id": self.document.id, "day": "2026-09-01", "label": "45",
                   "expected_sha256": "0" * 64, "expected_revision": 0,
                   "manual_total_amount": 6400, "reason": "核對簽單", "confirmed": True}
        endpoint = "/api/cost-hour-imports/grid/amount-adjustment"
        assert self.client.post(endpoint, json=payload).status_code == 409
        payload["expected_sha256"] = data["sha256"]
        worker = Employee(employee_code="D001", name="員工", role=Role.employee, bind_token="worker")
        self.session.add(worker)
        self.session.commit()
        app.dependency_overrides[get_current_actor] = lambda: worker
        assert self.client.post(endpoint, json=payload).status_code == 403
        self.upload.assert_not_called()
