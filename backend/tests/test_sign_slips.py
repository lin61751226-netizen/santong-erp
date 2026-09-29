from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import Employee, Role, SignSlipRecord, Worksite
from app.services.google_drive import google_drive_worklog_service


def _item(slip_no: str, slip_date: str, **overrides):
    base = {
        "slip_no": slip_no,
        "slip_date": slip_date,
        "customer_name": "齊裕營造",
        "site_code": "53",
        "location": "齊裕工地",
        "vehicles": {"twoPointFive": 1, "threePointZero": 1, "fourPointFive": 0, "truck": 0},
        "forklift_count": 2,
        "normal_hours": 16,
        "overtime_hours": 3,
        "total_hours": 19,
        "start_time": "08:00",
        "end_time": "19:00",
        "amount": 37000,
        "driver_names": "林建成",
        "customer_signature": "朱騰志",
        "work_content": "協助鐵帶、鐵管整理\n協助下玻璃",
        "notes": "由紙本謄錄",
    }
    base.update(overrides)
    return base


class TestSignSlips:
    def setup_method(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.owner = Employee(employee_code="BOSS001", name="老闆", bind_token="boss", role=Role.owner)
        self.admin = Employee(employee_code="ADMIN001", name="林金谷", bind_token="admin", role=Role.admin)
        self.worker = Employee(employee_code="D001", name="司機", bind_token="d1", role=Role.employee)
        self.site = Worksite(code="53", name="齊裕工地")
        self.session.add_all([self.owner, self.admin, self.worker, self.site])
        self.session.commit()

        app.dependency_overrides[get_session] = lambda: self.session
        app.dependency_overrides[get_current_actor] = lambda: self.owner
        self.client = TestClient(app)

    def teardown_method(self):
        app.dependency_overrides.clear()
        self.session.close()
        self.engine.dispose()

    def _batch(self, items, overwrite=False):
        return self.client.post("/api/sign-slips/batch",
                                json={"items": items, "overwrite": overwrite})

    def test_batch_create_and_list(self):
        response = self._batch([_item("0002761", "2026-09-01"),
                                _item("0002762", "2026-09-02")])
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["count"] == 2
        assert set(data["created"]) == {"0002761", "0002762"}

        listed = self.client.get("/api/sign-slips?year=2026&month=9").json()
        assert len(listed) == 2
        first = next(item for item in listed if item["slip_no"] == "0002761")
        assert first["vehicles"]["threePointZero"] == 1
        assert first["total_hours"] == 19
        assert first["amount"] == 37000
        assert first["customer_name"] == "齊裕營造"

    def test_batch_skips_existing_then_overwrites(self):
        self._batch([_item("0002761", "2026-09-01", amount=37000)])
        again = self._batch([_item("0002761", "2026-09-01", amount=18000)], overwrite=False)
        assert again.json()["skipped"] == ["0002761"]
        listed = self.client.get("/api/sign-slips?year=2026&month=9").json()
        assert listed[0]["amount"] == 37000

        overwrite = self._batch([_item("0002761", "2026-09-01", amount=18000)], overwrite=True)
        assert overwrite.json()["updated"] == ["0002761"]
        listed = self.client.get("/api/sign-slips?year=2026&month=9").json()
        assert listed[0]["amount"] == 18000

    def test_patch_and_delete(self):
        self._batch([_item("0002761", "2026-09-01")])
        slip_id = self.client.get("/api/sign-slips?year=2026&month=9").json()[0]["id"]
        patched = self.client.patch(f"/api/sign-slips/{slip_id}",
                                    json={"driver_names": "林建成、林忠", "overtime_hours": 4})
        assert patched.status_code == 200, patched.text
        assert patched.json()["sign_slip"]["driver_names"] == "林建成、林忠"

        deleted = self.client.delete(f"/api/sign-slips/{slip_id}")
        assert deleted.status_code == 200
        assert self.client.get("/api/sign-slips?year=2026&month=9").json() == []
        inactive = self.client.get("/api/sign-slips?year=2026&month=9&include_inactive=true").json()
        assert len(inactive) == 1
        assert inactive[0]["slip_no"] == "0002761"
        assert inactive[0]["is_active"] is False

    def test_writes_trigger_snapshot_backup(self):
        with patch.object(google_drive_worklog_service, "backup_database", new_callable=AsyncMock) as backup:
            backup.return_value = {"status": "saved"}
            created = self.client.post("/api/sign-slips", json=_item("0002761", "2026-09-01"))
            assert created.status_code == 201
            slip_id = created.json()["sign_slip"]["id"]
            assert created.json()["backup_status"] == "saved"

            skipped = self._batch([_item("0002761", "2026-09-01")])
            assert skipped.json()["backup_status"] == "no_change"

            patched = self.client.patch(f"/api/sign-slips/{slip_id}", json={"notes": "待核"})
            assert patched.json()["backup_status"] == "saved"

            deleted = self.client.delete(f"/api/sign-slips/{slip_id}")
            assert deleted.json()["backup_status"] == "saved"
            assert backup.await_count == 3

    def test_duplicate_slip_no_rejected(self):
        self._batch([_item("0002761", "2026-09-01")])
        response = self.client.post("/api/sign-slips", json=_item("0002761", "2026-09-08"))
        assert response.status_code == 400

    def test_employee_forbidden(self):
        app.dependency_overrides[get_current_actor] = lambda: self.worker
        response = self._batch([_item("0002761", "2026-09-01")])
        assert response.status_code == 403

    def test_eight_saved_slips_appear_on_each_day_in_site_53_journal(self):
        source = Path(__file__).parents[2] / "sign_slips_0901_0908.json"
        items = json.loads(source.read_text(encoding="utf-8"))["items"]
        imported = self._batch(items)
        assert imported.status_code == 200, imported.text
        assert len(imported.json()["created"]) == 8

        for source_item in items:
            day = source_item["slip_date"]
            response = self.client.get(f"/api/worksite-journals?target_date={day}")
            assert response.status_code == 200, response.text
            sites = response.json()["sites"]
            assert len(sites) == 1
            assert sites[0]["site_id"] == self.site.id
            assert sites[0]["site_code"] == "53"
            slips = sites[0]["sign_slips"]
            assert len(slips) == 1
            assert slips[0]["slip_no"] == source_item["slip_no"]
            assert slips[0]["work_content"] == source_item["work_content"]

        saved = self.session.exec(select(SignSlipRecord)).all()
        assert len(saved) == 8
        changed = self.client.patch(f"/api/sign-slips/{saved[0].id}", json={"work_content": "人工修正後內容"})
        assert changed.status_code == 200, changed.text
        refreshed = self.client.get("/api/worksite-journals?target_date=2026-09-01").json()
        assert refreshed["sites"][0]["sign_slips"][0]["work_content"] == "人工修正後內容"

        manager = Employee(
            employee_code="M001", name="53主管", bind_token="m1",
            role=Role.site_manager, home_site_id=self.site.id,
        )
        self.session.add(manager)
        self.session.commit()
        app.dependency_overrides[get_current_actor] = lambda: manager
        scoped = self.client.get("/api/worksite-journals?target_date=2026-09-01")
        assert scoped.status_code == 200
        assert scoped.json()["sites"][0]["sign_slips"][0]["slip_no"] == "0002761"

    def test_journal_existing_slip_button_opens_saved_edit_form(self):
        template = (Path(__file__).parents[1] / "app" / "templates" / "index.html").read_text(encoding="utf-8")
        assert 'onclick="openJournalSignSlip(${siteIndex})"' in template
        assert "const saved = site.sign_slips || [];" in template
        assert 'const current = signSlipCache.find(item => item.id === selected.id);' in template
        assert 'fillSignSlipForm(current);' in template
        assert 'setWorkspace("documents");' in template
        assert 'onclick="printSingleSignSlip(${row.id})"' in template
        assert 'function printSignSlips(rows = signSlipCache)' in template
