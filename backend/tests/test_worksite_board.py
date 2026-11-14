"""Selected worksite board must not mutate business records or leak another site."""
from datetime import date, datetime, time

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import (
    AssignmentMember, AttendanceEvent, Employee, Forklift,
    GroupTextLog, LeaveRequest, LeaveStatus, PhotoUploadLog, Role,
    SignSlipRecord, WorkAssignment, WorkHourImportLog, Worksite,
    WorksiteJournalHours,
)


DAY = date(2026, 10, 9)


def test_site_day_readonly_boundary_and_pricing_review():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        owner = Employee(employee_code="BOSS101", name="老闆", bind_token="owner", role=Role.owner)
        manager = Employee(employee_code="MGR101", name="主任", bind_token="manager", role=Role.site_manager)
        worker = Employee(employee_code="EMP101", name="司機", bind_token="worker", role=Role.employee)
        absent = Employee(employee_code="EMP102", name="請假員工", bind_token="absent", role=Role.employee)
        site = Worksite(code="53", name="齊裕53")
        other = Worksite(code="47", name="善捷47")
        session.add_all([owner, manager, worker, absent, site, other])
        session.commit()
        for row in (owner, manager, worker, absent, site, other):
            session.refresh(row)
        manager.home_site_id = site.id
        work = WorkAssignment(work_date=DAY, site_id=site.id, work_item="移料", start_time=time(8))
        session.add(work)
        session.commit()
        session.refresh(work)
        session.add_all([
            AssignmentMember(assignment_id=work.id, employee_id=worker.id),
            AssignmentMember(assignment_id=work.id, employee_id=absent.id),
            LeaveRequest(employee_id=absent.id, leave_type="事假", status=LeaveStatus.approved,
                         start_date=DAY, end_date=DAY, reason="已核准"),
            AttendanceEvent(employee_id=worker.id, site_id=site.id, event_type="上班打卡",
                            happened_at=datetime(2026, 10, 8, 23, 50)),
            AttendanceEvent(employee_id=worker.id, site_id=site.id, event_type="下班打卡",
                            happened_at=datetime(2026, 10, 9, 8, 50)),
            AttendanceEvent(employee_id=worker.id, site_id=other.id, event_type="上班打卡",
                            happened_at=datetime(2026, 10, 9, 1, 0)),
            PhotoUploadLog(employee_id=worker.id, site_id=site.id, file_name="work.jpg",
                           drive_file_id="file1", drive_folder_id="folder", drive_url="https://drive.google.com/file1",
                           work_date=DAY),
            PhotoUploadLog(employee_id=worker.id, site_id=other.id, file_name="other.jpg",
                           drive_file_id="file2", drive_folder_id="folder", drive_url="https://drive.google.com/file2",
                           work_date=DAY),
            GroupTextLog(source_type="group", source_id="g", site_id=site.id,
                         content="搬運材料", sent_at=datetime(2026, 10, 9, 1, 0)),
            WorksiteJournalHours(work_date=DAY, worksite_id=site.id, normal_hours=8),
            SignSlipRecord(slip_no="0001", slip_date=DAY, worksite_id=site.id,
                           updated_at=datetime(2026, 10, 10, 1, 0)),
            Forklift(forklift_code="2號", current_site_id=site.id),
        ])
        session.commit()
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_actor] = lambda: owner
        try:
            client = TestClient(app)
            before = len(session.exec(select(GroupTextLog)).all())
            response = client.get(f"/api/worksite-board?target_date={DAY}&site_id={site.id}")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            body = response.json()
            assert body["assignments"]["rows"][0]["work_item"] == "移料"
            assert body["attendance"]["missing_names"] == []
            assert len(body["attendance"]["rows"]) == 2
            assert body["photos"]["count"] == 1
            assert body["journal"]["group_text_count"] == 1
            assert body["journal"]["saved_hours"]["normal"] == 8
            assert body["sign_slips"]["rows"][0]["number"] == "0001"
            assert body["pricing"]["state"] == "待核對"
            assert body["inspections"]["missing_machines"] == []
            assert len(session.exec(select(GroupTextLog)).all()) == before

            app.dependency_overrides[get_current_actor] = lambda: manager
            assert client.get(f"/api/worksite-board?target_date={DAY}&site_id={site.id}").status_code == 200
            assert client.get(f"/api/worksite-board?target_date={DAY}&site_id={other.id}").status_code == 403
            app.dependency_overrides[get_current_actor] = lambda: worker
            assert client.get(f"/api/worksite-board?target_date={DAY}&site_id={site.id}").status_code == 403
        finally:
            app.dependency_overrides.clear()
    engine.dispose()


def test_pricing_import_is_not_treated_as_current_after_slip_change():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        site = Worksite(code="53", name="齊裕53")
        session.add(site)
        session.commit()
        session.refresh(site)
        from app.services.worksite_board import build_worksite_board
        session.add(SignSlipRecord(slip_no="0002", slip_date=DAY, worksite_id=site.id,
                                   updated_at=datetime(2026, 10, 10, 1)))
        session.add(WorkHourImportLog(
            managed_document_id=1, worksite_id=site.id, work_date=DAY,
            worksheet_name="53", target_label="53", target_row=10,
            drive_file_id="file", drive_url="https://drive.google.com/file",
            created_at=datetime(2026, 10, 9, 1),
        ))
        session.commit()
        board = build_worksite_board(session, DAY, site)
        assert board["pricing"]["state"] == "簽單較匯入紀錄新，需重新核對"
        assert board["pricing"]["import_count"] == 1
    engine.dispose()
