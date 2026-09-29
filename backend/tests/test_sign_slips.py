from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import Employee, Role, Worksite
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
