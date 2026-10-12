from datetime import date, datetime, time, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core import local_time
from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import (
    AssignmentMember, AttendanceEvent, Employee, Forklift, ForkliftInspection,
    Role, WorkAssignment, WorkReportEvent, Worksite,
)
from app.services import forklift_service, hr
from app.services.line import line_service, process_webhook_event

DAY = date(2026, 10, 12)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 10, 11, 23, 51, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


@pytest.fixture
def records():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        site = Worksite(code="53", name="齊裕53")
        employee = Employee(employee_code="TEST", name="測試員工", bind_token="test", line_user_id="U-test", role=Role.admin)
        session.add_all([site, employee])
        session.commit()
        assignment = WorkAssignment(work_date=DAY, site_id=site.id, work_item="堆高機", start_time=time(8))
        session.add(assignment)
        session.commit()
        session.add(AssignmentMember(assignment_id=assignment.id, employee_id=employee.id))
        session.commit()
        with patch.object(local_time, "datetime", FrozenDatetime):
            yield session, employee, site, assignment
        forklift_service.clear_session("U-test")
        app.dependency_overrides.clear()
    engine.dispose()


def test_business_date_and_half_open_utc_bounds(records):
    assert local_time.local_today() == DAY
    assert local_time.utc_day_bounds(DAY) == (datetime(2026, 10, 11, 16), datetime(2026, 10, 12, 16))
    assert local_time.as_local(datetime(2026, 10, 11, 23, 51)).date() == DAY


def test_clock_in_before_utc_midnight_uses_today_assignment(records):
    session, employee, site, assignment = records
    result = hr.record_attendance_event(session, employee, "上班打卡")
    assert result.event.assignment_id == assignment.id
    assert result.event.site_id == site.id
    assert employee.line_user_id == "U-test"


def test_attendance_history_and_arrival_use_taipei_day(records):
    session, employee, site, assignment = records
    event = AttendanceEvent(employee_id=employee.id, site_id=site.id, event_type="到達工地", happened_at=datetime(2026, 10, 11, 23, 51))
    session.add(event)
    session.add(AttendanceEvent(employee_id=employee.id, site_id=site.id, event_type="下班打卡", happened_at=datetime(2026, 10, 12, 16)))
    session.commit()
    assert hr.get_today_arrival_site(session, employee.id).id == site.id
    assert hr.get_latest_attendance_event(session, employee.id, DAY).id == event.id
    rows = hr.build_attendance_rows(session, [employee], DAY)
    assert len(rows[0]["events"]) == 1
    assert rows[0]["work_date"] == "2026-10-12"
    assert "遲到" not in rows[0]["anomalies"]
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_actor] = lambda: employee
    response = TestClient(app).get("/api/attendance", params={"employee_code": "TEST"})
    assert response.status_code == 200
    matching = [row for row in response.json() if row["work_date"] == "2026-10-12"]
    assert any(item["id"] == event.id for item in matching[0]["events"])
    assert event.happened_at == datetime(2026, 10, 11, 23, 51)


def test_report_date_and_filter_preserve_morning_record(records):
    session, employee, site, _ = records
    report = WorkReportEvent(employee_id=employee.id, site_id=site.id, event_type="工作開始", reported_at=datetime(2026, 10, 11, 23, 51))
    session.add(report)
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_actor] = lambda: employee
    response = TestClient(app).get("/api/work-reports", params={"date_from": DAY.isoformat(), "date_to": DAY.isoformat()})
    assert response.status_code == 200
    assert response.json()[0]["work_date"] == DAY.isoformat()
    assert response.json()[0]["id"] == report.id


def test_forklift_stats_include_clock_in_before_utc_midnight(records):
    session, employee, site, _ = records
    vehicle = Forklift(forklift_code="1號", model="3噸")
    session.add(vehicle)
    session.commit()
    session.add(ForkliftInspection(forklift_id=vehicle.id, operator_id=employee.id, site_id=site.id, inspection_date=DAY))
    session.add_all([
        AttendanceEvent(employee_id=employee.id, site_id=site.id, event_type="上班打卡", happened_at=datetime(2026, 10, 11, 23, 51)),
        AttendanceEvent(employee_id=employee.id, site_id=site.id, event_type="下班打卡", happened_at=datetime(2026, 10, 12, 8, 51)),
    ])
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_actor] = lambda: employee
    response = TestClient(app).get("/api/forklift-attendance/stats", params={"date_from": DAY.isoformat(), "date_to": DAY.isoformat()})
    assert response.status_code == 200
    assert response.json()["summary"]["total_hours"] == 9
    assert response.json()["rows"][0]["check_in"] == "2026-10-11T23:51:00"


@pytest.mark.parametrize("count", [1, 13, 14, 35])
def test_inspection_pages_keep_all_choices_within_line_limit(count):
    choices = [(f"工地{i}", f"點檢工地:{i}") for i in range(count)]
    seen = []
    for page in range((count + 9) // 10):
        message = forklift_service.selection_message("請選工地", choices, "點檢工地頁", page)
        items = message["quickReply"]["items"]
        assert len(items) <= 13
        assert all(len(item["action"]["label"]) <= 20 for item in items)
        seen.extend(item["action"]["text"] for item in items if item["action"]["text"].startswith("點檢工地:"))
    assert seen == [command for _, command in choices]


def test_inspection_can_finish_after_second_page_selection(records):
    import asyncio

    session, employee, site, _ = records
    for i in range(13):
        session.add(Worksite(code=f"SITE{i}", name=f"A{i:02d}"))
    vehicle = Forklift(forklift_code="1號", model="3噸", current_site_id=site.id)
    session.add(vehicle)
    session.commit()
    reply = AsyncMock()

    async def send(text):
        await process_webhook_event(session, {
            "type": "message", "replyToken": "test", "source": {"type": "user", "userId": "U-test"},
            "message": {"type": "text", "text": text},
        })

    with (
        patch.object(line_service, "reply_messages", reply),
        patch.object(line_service, "reply_text", AsyncMock()),
        patch("app.services.line._backup_preserved_records", AsyncMock()),
        patch("app.services.forklift_notifications.deliver_forklift_notifications", AsyncMock()),
        patch.object(forklift_service, "local_today", return_value=DAY),
    ):
        asyncio.run(send("點檢"))
        first = reply.call_args.args[1][0]["quickReply"]["items"]
        assert len(first) <= 13
        assert any(item["action"]["text"] == "點檢工地頁:1" for item in first)
        asyncio.run(send("點檢工地頁:1"))
        second = reply.call_args.args[1][0]["quickReply"]["items"]
        assert any(item["action"]["text"] == f"點檢工地:{site.id}" for item in second)
        asyncio.run(send(f"點檢工地:{site.id}"))
        asyncio.run(send(f"點檢堆高機:{vehicle.id}"))
        for _ in range(10):
            asyncio.run(send("正常"))
    inspection = session.exec(select(ForkliftInspection)).one()
    assert inspection.inspection_date == DAY
    assert inspection.site_id == site.id
    assert inspection.operator_id == employee.id
    assert len(inspection.inspection_items) == 10
