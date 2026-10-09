"""今日總覽只讀彙整：台灣日界、權限與不快取。"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import (
    AiBillingCheck,
    AiJournalDraft,
    AiSignSlipDraft,
    AssignmentMember,
    AssignmentStatus,
    AttendanceEvent,
    Employee,
    EmployeeStatus,
    Forklift,
    ForkliftInspection,
    ForkliftStatus,
    LeaveRequest,
    LeaveStatus,
    Role,
    SignSlipRecord,
    WorkAssignment,
    WorkReportEvent,
    Worksite,
    WorksiteJournalHours,
)
from app.services.google_drive import google_drive_worklog_service
from app.services.today_overview import build_today_overview

TAIPEI = ZoneInfo("Asia/Taipei")
DAY = datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc)  # 10:00 in Taipei
EDGE = datetime(2026, 10, 8, 16, 30, tzinfo=timezone.utc)  # 00:30 in Taipei


def taipei_to_utc(hour: int, minute: int, day: datetime = DAY) -> datetime:
    local = datetime(2026, 10, 9, hour, minute, tzinfo=TAIPEI)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


class TestTodayOverview:
    def setup_method(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.owner = Employee(employee_code="BOSS001", name="老闆", bind_token="boss", role=Role.owner, status=EmployeeStatus.active)
        self.worker = Employee(employee_code="EMP001", name="勝忠", bind_token="winner", role=Role.employee, title="堆高機司機", status=EmployeeStatus.active)
        self.late = Employee(employee_code="EMP003", name="建成", bind_token="ray", role=Role.employee, title="堆高機司機", status=EmployeeStatus.active)
        self.missing = Employee(employee_code="EMP005", name="林小咪", bind_token="mimi", role=Role.employee, title="堆高機司機", status=EmployeeStatus.active)
        self.clerk = Employee(employee_code="ADMIN002", name="秀蓉", bind_token="clerk", role=Role.admin, status=EmployeeStatus.active)
        self.qiyu = Worksite(code="53", name="齊裕53")
        self.shan = Worksite(code="47", name="善捷47")
        self.machine = Forklift(forklift_code="2號", model="2.5噸", status=ForkliftStatus.operating)
        self.session.add_all([self.owner, self.worker, self.late, self.missing, self.clerk, self.qiyu, self.shan, self.machine])
        self.session.commit()
        for row in (self.owner, self.worker, self.late, self.missing, self.clerk, self.qiyu, self.shan, self.machine):
            self.session.refresh(row)
        self.machine.current_site_id = self.qiyu.id
        self.session.add(self.machine)
        self._seed_day()
        app.dependency_overrides[get_session] = lambda: self.session
        app.dependency_overrides[get_current_actor] = lambda: self.owner
        self.client = TestClient(app)

    def teardown_method(self):
        app.dependency_overrides.clear()
        self.session.close()
        self.engine.dispose()

    def _seed_day(self):
        first = WorkAssignment(
            work_date=DAY.astimezone(TAIPEI).date(), site_id=self.qiyu.id, work_item="推磁磚",
            start_time=time(8, 0), end_time=time(17, 0), vehicle="2號", status=AssignmentStatus.scheduled,
        )
        second = WorkAssignment(
            work_date=DAY.astimezone(TAIPEI).date(), site_id=self.shan.id, work_item="物料搬運",
            start_time=time(8, 0), vehicle="3號", status=AssignmentStatus.scheduled,
        )
        cancelled = WorkAssignment(
            work_date=DAY.astimezone(TAIPEI).date(), site_id=self.qiyu.id, work_item="取消的工作",
            status=AssignmentStatus.cancelled,
        )
        self.session.add_all([first, second, cancelled])
        self.session.commit()
        for row in (first, second):
            self.session.refresh(row)
        self.session.add(AssignmentMember(assignment_id=first.id, employee_id=self.worker.id))
        self.session.add(AssignmentMember(assignment_id=first.id, employee_id=self.late.id))
        self.session.add(AssignmentMember(assignment_id=second.id, employee_id=self.missing.id))
        self.session.add(AttendanceEvent(employee_id=self.worker.id, site_id=self.qiyu.id, event_type="上班打卡", happened_at=taipei_to_utc(7, 50)))
        self.session.add(AttendanceEvent(employee_id=self.late.id, site_id=self.qiyu.id, event_type="上班打卡", happened_at=taipei_to_utc(8, 40)))
        self.session.add(AttendanceEvent(employee_id=self.worker.id, site_id=self.qiyu.id, event_type="上班打卡", happened_at=datetime(2026, 10, 8, 15, 0)))
        self.session.add(WorkReportEvent(employee_id=self.late.id, site_id=self.qiyu.id, event_type="異常回報", note="煞車異常", reported_at=taipei_to_utc(9, 5)))
        self.session.add(ForkliftInspection(
            forklift_id=self.machine.id, operator_id=self.worker.id, site_id=self.qiyu.id,
            inspection_date=DAY.astimezone(TAIPEI).date(), all_passed=False, notes="剎車偏軟",
        ))
        self.session.add(LeaveRequest(
            employee_id=self.clerk.id, leave_type="排休", status=LeaveStatus.pending,
            start_date=DAY.astimezone(TAIPEI).date() + timedelta(days=3),
            end_date=DAY.astimezone(TAIPEI).date() + timedelta(days=3), reason="行政排休",
        ))
        self.session.add(WorksiteJournalHours(work_date=DAY.astimezone(TAIPEI).date(), worksite_id=self.qiyu.id, normal_hours=8))
        self.session.add(SignSlipRecord(
            slip_no="0009001", slip_date=DAY.astimezone(TAIPEI).date(), worksite_id=self.qiyu.id,
            customer_name="齊裕營造", is_active=True,
        ))
        self.session.add(AiJournalDraft(work_date=DAY.astimezone(TAIPEI).date(), worksite_id=self.qiyu.id, status="draft", content={}))
        self.session.add(AiJournalDraft(work_date=DAY.astimezone(TAIPEI).date(), worksite_id=self.shan.id, status="superseded", content={}))
        self.session.add(AiSignSlipDraft(work_date=DAY.astimezone(TAIPEI).date(), worksite_id=self.shan.id, status="draft", customer_name="未知客戶"))
        self.session.add(AiBillingCheck(
            month="2026-10", discrepancy_count=2, report={"rows": [
                {"site_name": "善捷47", "work_date": "2026-10-09", "explanation": "有派工，尚未有簽單。"},
                {"site_name": "齊裕53", "work_date": "2026-10-09", "explanation": "日誌與簽單時數待人工核對。"},
            ]},
        ))
        self.session.commit()

    def test_taipei_day_counts_attendance_reviews_and_anomalies(self):
        data = build_today_overview(self.session, now=DAY)
        assert data["today"] == "2026-10-09"
        assert data["timezone"] == "Asia/Taipei"
        attendance = data["attendance"]
        assert attendance["in_count"] == 2
        assert [row["name"] for row in attendance["clocked_in"]] == ["勝忠", "建成"]
        assert attendance["clocked_in"][0]["time"] == "07:50"
        assert [row["name"] for row in attendance["late"]] == ["建成"]
        assert [row["name"] for row in attendance["missing"]] == ["林小咪"]
        assert data["assignments"]["count"] == 2
        sites = {row["site_name"]: row for row in data["assignments"]["sites"]}
        assert sites["齊裕53"]["people"] == ["勝忠", "建成"]
        assert "2號" in sites["齊裕53"]["forklifts"]
        assert sites["善捷47"]["people"] == ["林小咪"]
        reviews = data["reviews"]
        assert reviews["leave_count"] == 1
        assert reviews["journal_draft_count"] == 1
        assert reviews["sign_slip_draft_count"] == 1
        assert reviews["billing_issue_count"] == 2
        assert reviews["total"] == 5
        anomalies = data["anomalies"]
        assert [row["name"] for row in anomalies["missed_inspections"]] == ["建成", "林小咪"]
        assert anomalies["failed_inspections"][0]["forklift_code"] == "2號"
        assert anomalies["abnormal_reports"][0]["detail"] == "煞車異常"
        assert anomalies["missing_journals"] == [{"site_name": "善捷47"}]
        assert anomalies["missing_sign_slips"] == [{"site_name": "善捷47"}]
        assert anomalies["panels"]["journal"] == "journal"
        assert anomalies["panels"]["sign_slip"] == "sign-slips"

    def test_midnight_window_uses_taipei_bounds_and_does_not_call_anyone_missing_yet(self):
        inside = AttendanceEvent(
            employee_id=self.clerk.id, event_type="到達工地", happened_at=datetime(2026, 10, 8, 16, 40),
        )
        self.session.add(inside)
        self.session.commit()
        data = build_today_overview(self.session, now=EDGE)
        names = [row["name"] for row in data["attendance"]["clocked_in"]]
        assert "秀蓉" in names
        assert data["attendance"]["missing_count"] == 0
        assert data["today"] == "2026-10-09"

    def test_approved_leave_is_not_counted_as_missing(self):
        self.session.add(LeaveRequest(
            employee_id=self.missing.id, leave_type="事假", status=LeaveStatus.approved,
            start_date=DAY.astimezone(TAIPEI).date(), end_date=DAY.astimezone(TAIPEI).date(), reason="請假",
        ))
        self.session.commit()
        data = build_today_overview(self.session, now=DAY)
        assert [row["name"] for row in data["attendance"]["missing"]] == []
        assert any(row["name"] == "林小咪" for row in data["attendance"]["on_leave"])

    def test_configured_drive_without_an_alert_adds_no_warning(self, monkeypatch):
        monkeypatch.setattr(google_drive_worklog_service, "is_configured", lambda: True)
        monkeypatch.setattr(google_drive_worklog_service, "_has_oauth", lambda: True)
        monkeypatch.setattr("app.services.today_overview.settings.google_drive_worklog_folder_id", "1234567890")
        data = build_today_overview(self.session, now=DAY)
        assert data["anomalies"]["drive_warning_count"] == 0

    def test_endpoint_is_owner_admin_readonly_and_not_stored(self):
        before = len(self.session.exec(select(AttendanceEvent)).all())
        response = self.client.get("/api/today-overview")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["today"] == datetime.now(TAIPEI).date().isoformat()
        assert body["attendance"]["panel"] == "attendance"
        assert len(self.session.exec(select(AttendanceEvent)).all()) == before

        app.dependency_overrides[get_current_actor] = lambda: self.worker
        forbidden = self.client.get("/api/today-overview")
        assert forbidden.status_code == 403

        app.dependency_overrides.pop(get_current_actor)
        anonymous = self.client.get("/api/today-overview")
        assert anonymous.status_code == 401
