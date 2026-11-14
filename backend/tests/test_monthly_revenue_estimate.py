"""The daily revenue estimate uses saved pricing, not imported cash receipts."""
from datetime import date

from fastapi.testclient import TestClient
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import Employee, ManagedDocument, Role, Worksite, WorksiteForkliftCharge
from app.routes import admin


def test_monthly_estimate_counts_saved_amounts_once_and_reports_partial(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        owner = Employee(employee_code="BOSS501", name="老闆", bind_token="owner501", role=Role.owner)
        worker = Employee(employee_code="EMP501", name="員工", bind_token="worker501", role=Role.employee)
        site = Worksite(code="CW", name="國泰慈文")
        session.add_all([owner, worker, site])
        session.commit()
        for item in (owner, worker, site):
            session.refresh(item)
        session.add_all([
            WorksiteForkliftCharge(worksite_id=site.id, work_date=date(2026, 11, 14),
                                   vehicle_type="2.5T", units=1, normal_amount=8000,
                                   overtime_amount=1000, manual_total_amount=6400,
                                   last_request_id="estimate-today", updated_by_id=owner.id),
            WorksiteForkliftCharge(worksite_id=site.id, work_date=date(2026, 11, 15),
                                   vehicle_type="2.5T", units=1, normal_amount=8000,
                                   last_request_id="estimate-future", updated_by_id=owner.id),
        ])
        session.commit()
        monkeypatch.setattr(admin, "local_today", lambda: date(2026, 11, 14))
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_actor] = lambda: owner
        try:
            client = TestClient(app)
            partial = client.get("/api/monthly-revenue-estimate")
            assert partial.status_code == 200
            assert partial.headers["cache-control"] == "no-store"
            assert partial.json()["complete"] is False
            assert partial.json()["site_amount"] == 6400
            assert partial.json()["known_total"] == 6400

            session.add(ManagedDocument(category="推高機計價", title="11511", original_file_name="11511.xlsm",
                                        stored_file_name="11511.xlsm", drive_file_id="estimate-file",
                                        drive_folder_id="folder", drive_url="https://drive.google.com/estimate-file"))
            session.commit()

            async def saved_grid(**kwargs):
                return {"sheets": [{"rows": [
                    {"day": "2026-11-13", "effective_total": 12000},
                    {"day": "2026-11-14", "effective_total": 6000},
                    {"day": "2026-11-15", "effective_total": 9000},
                ]}]}

            monkeypatch.setattr(admin, "read_cost_hour_grid", saved_grid)
            saved = client.get("/api/monthly-revenue-estimate")
            assert saved.status_code == 200
            assert saved.json()["legacy_amount"] == 18000
            assert saved.json()["known_total"] == 24400
            assert saved.json()["legacy_document"] == "11511.xlsm"
            assert saved.json()["tax_included"] is False
            assert len(session.exec(select(WorksiteForkliftCharge)).all()) == 2

            async def unavailable_grid(**kwargs):
                raise HTTPException(502, "計價原檔暫時無法讀取")

            monkeypatch.setattr(admin, "read_cost_hour_grid", unavailable_grid)
            unavailable = client.get("/api/monthly-revenue-estimate")
            assert unavailable.status_code == 200
            assert unavailable.json()["complete"] is False
            assert unavailable.json()["legacy_amount"] is None
            assert unavailable.json()["known_total"] == 6400
            assert "暫時無法讀取" in unavailable.json()["issue"]

            app.dependency_overrides[get_current_actor] = lambda: worker
            assert client.get("/api/monthly-revenue-estimate").status_code == 403
        finally:
            app.dependency_overrides.clear()
    engine.dispose()
