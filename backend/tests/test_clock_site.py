from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.config import Settings
from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import (
    AdminAuditLog,
    AssignmentMember,
    AttendanceEvent,
    AttendanceSiteRevision,
    Employee,
    Role,
    WorkAssignment,
    Worksite,
)
from app.services.hr import build_attendance_rows, record_attendance_event
from app.services.line import (
    _pending_clock_site,
    _pending_location_attendance,
    line_service,
    process_webhook_event,
)
from app.services.speech_correction import spoken_numbers


JINJUN = (25.04740, 121.38739)


class ClockSiteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.backup_patcher = patch(
            "app.services.line.google_drive_worklog_service.backup_database",
            new=AsyncMock(return_value={"status": "saved"}),
        )
        self.backup_database = self.backup_patcher.start()
        _pending_location_attendance.clear()
        _pending_clock_site.clear()

    def tearDown(self) -> None:
        _pending_location_attendance.clear()
        _pending_clock_site.clear()
        self.backup_patcher.stop()
        app.dependency_overrides.clear()
        self.engine.dispose()

    def _sites(self, session: Session, *, assigned_coords=True, jinjun_coords=True):
        assigned = Worksite(
            code="六和",
            name="六和機械",
            latitude=25.0 if assigned_coords else None,
            longitude=121.0 if assigned_coords else None,
            is_active=True,
        )
        jinjun = Worksite(
            code="金駿76",
            name="金駿76",
            latitude=JINJUN[0] if jinjun_coords else None,
            longitude=JINJUN[1] if jinjun_coords else None,
            is_active=True,
        )
        blank = Worksite(code="空白", name="尚未定位", is_active=True)
        employee = Employee(
            employee_code="BOSS001",
            name="三通工程行林老闆",
            bind_token="ST-BOSS",
            role=Role.owner,
            line_user_id="U-boss",
        )
        session.add_all([assigned, jinjun, blank, employee])
        session.commit()
        return assigned, jinjun, blank, employee

    def _assign(self, session: Session, employee: Employee, site: Worksite) -> WorkAssignment:
        assignment = WorkAssignment(
            work_date=date.today(),
            site_id=site.id,
            work_item="堆高機",
        )
        session.add(assignment)
        session.commit()
        session.add(AssignmentMember(assignment_id=assignment.id, employee_id=employee.id))
        session.commit()
        return assignment

    def test_radius_setting_uses_four_hundred_meters_when_blank_or_invalid(self) -> None:
        self.assertEqual(Settings(clock_site_radius_m="").clock_site_radius_m, 400)
        self.assertEqual(Settings(clock_site_radius_m="12").clock_site_radius_m, 400)
        self.assertEqual(Settings(clock_site_radius_m="nope").clock_site_radius_m, 400)
        self.assertEqual(Settings(clock_site_radius_m="450").clock_site_radius_m, 450)

    def test_clock_on_assigned_site_records_that_site(self) -> None:
        with Session(self.engine) as session:
            assigned, _jinjun, _blank, employee = self._sites(session)
            assigned.latitude, assigned.longitude = JINJUN
            assigned.geofence_radius_m = 50
            session.add(assigned)
            session.commit()
            self._assign(session, employee, assigned)
            result = record_attendance_event(
                session, employee, "上班打卡",
                latitude=JINJUN[0] + 0.0015, longitude=JINJUN[1],
                note="桃園市龜山區",
            )
            self.assertFalse(result.needs_confirmation)
            self.assertEqual(result.event.site_id, assigned.id)
            self.assertEqual(result.event.matched_site_id, assigned.id)
            self.assertLess(result.event.distance_m, 400)
            self.assertAlmostEqual(result.event.latitude, JINJUN[0] + 0.0015)
            self.assertNotIn("打卡工地與派工不符", "；".join(result.anomalies))
            self.assertIn("桃園市龜山區", result.event.note)

    def test_different_site_waits_for_confirmation_then_records_the_choice(self) -> None:
        with Session(self.engine) as session:
            assigned, jinjun, _blank, employee = self._sites(session)
            self._assign(session, employee, assigned)
            pending = record_attendance_event(
                session, employee, "上班打卡",
                latitude=JINJUN[0], longitude=JINJUN[1], note="文化一路",
            )
            self.assertTrue(pending.needs_confirmation)
            self.assertIsNone(pending.event)
            self.assertEqual(session.exec(select(AttendanceEvent)).all(), [])
            self.assertEqual(pending.plan.matched_site_name, "金駿76")

            confirmed = record_attendance_event(
                session, employee, "上班打卡",
                latitude=JINJUN[0], longitude=JINJUN[1], note="文化一路",
                confirmed_choice="yes",
            )
            self.assertEqual(confirmed.event.site_id, jinjun.id)
            self.assertEqual(confirmed.event.matched_site_id, jinjun.id)
            self.assertAlmostEqual(confirmed.event.latitude, JINJUN[0])
            self.assertAlmostEqual(confirmed.event.longitude, JINJUN[1])
            self.assertLess(confirmed.event.distance_m, 5)
            self.assertIn("打卡工地與派工不符", confirmed.anomalies)
            self.assertIn("文化一路", confirmed.event.note)
            rows = build_attendance_rows(session, [employee], date.today())
            self.assertIn("打卡工地與派工不符", rows[0]["anomalies"])

            stayed = record_attendance_event(
                session, employee, "下班打卡",
                latitude=JINJUN[0], longitude=JINJUN[1],
                confirmed_choice="assigned",
            )
            self.assertEqual(stayed.event.site_id, assigned.id)
            self.assertNotIn("打卡工地與派工不符", stayed.anomalies)
            self.assertTrue(any("定位不在派工工地範圍" in item for item in stayed.anomalies))

    def test_out_of_range_keeps_assignment_and_records_distance(self) -> None:
        with Session(self.engine) as session:
            assigned, jinjun, _blank, employee = self._sites(session)
            assigned.latitude, assigned.longitude = JINJUN
            session.add(assigned)
            session.commit()
            self._assign(session, employee, assigned)
            result = record_attendance_event(
                session, employee, "上班打卡",
                latitude=JINJUN[0], longitude=JINJUN[1] + 0.02,
            )
            self.assertFalse(result.needs_confirmation)
            self.assertEqual(result.event.site_id, assigned.id)
            self.assertIsNone(result.event.matched_site_id)
            self.assertGreater(result.event.distance_m, 400)
            self.assertTrue(any("定位不在任何工地範圍" in item and "公尺" in item for item in result.anomalies))
            self.assertNotEqual(result.event.site_id, jinjun.id)

    def test_site_without_coordinates_is_not_guessed(self) -> None:
        with Session(self.engine) as session:
            assigned, jinjun, _blank, employee = self._sites(session, assigned_coords=False)
            jinjun.latitude, jinjun.longitude = 24.0, 120.0
            session.add(jinjun)
            session.commit()
            self._assign(session, employee, assigned)
            result = record_attendance_event(
                session, employee, "上班打卡", latitude=JINJUN[0], longitude=JINJUN[1],
            )
            self.assertFalse(result.needs_confirmation)
            self.assertEqual(result.event.site_id, assigned.id)
            self.assertIsNone(result.event.matched_site_id)
            self.assertTrue(any("尚未設定座標" in item for item in result.anomalies))
            self.assertTrue(any("定位不在任何工地範圍" in item for item in result.anomalies))

    def test_no_assignment_confirms_nearest_site_or_records_none(self) -> None:
        with Session(self.engine) as session:
            _assigned, jinjun, _blank, employee = self._sites(session)
            pending = record_attendance_event(
                session, employee, "上班打卡", latitude=JINJUN[0], longitude=JINJUN[1],
            )
            self.assertTrue(pending.needs_confirmation)
            self.assertIsNone(pending.plan.assigned_site_id)
            yes = record_attendance_event(
                session, employee, "上班打卡", latitude=JINJUN[0], longitude=JINJUN[1],
                confirmed_choice="yes",
            )
            self.assertEqual(yes.event.site_id, jinjun.id)
            self.assertIn("今日尚未排定工作，已先記錄打卡事件。", yes.anomalies)

            denied = record_attendance_event(
                session, employee, "下班打卡", latitude=JINJUN[0], longitude=JINJUN[1],
                confirmed_choice="none",
            )
            self.assertIsNone(denied.event.site_id)
            self.assertTrue(any("員工表示不在最近工地" in item for item in denied.anomalies))
            self.assertIn("今日尚未排定工作，已先記錄打卡事件。", denied.anomalies)

    async def test_line_location_asks_before_saving_a_different_site(self) -> None:
        with Session(self.engine) as session:
            assigned, jinjun, _blank, employee = self._sites(session)
            self._assign(session, employee, assigned)
            replies = AsyncMock(return_value=(True, "sent"))
            texts = AsyncMock(return_value=(True, "sent"))
            start = {
                "type": "message", "replyToken": "reply-start",
                "source": {"type": "user", "userId": "U-boss"},
                "message": {"type": "text", "text": "上班打卡"},
            }
            location = {
                "type": "message", "replyToken": "reply-location",
                "source": {"type": "user", "userId": "U-boss"},
                "message": {
                    "type": "location",
                    "latitude": JINJUN[0],
                    "longitude": JINJUN[1],
                    "address": "桃園市龜山區樂善里文化一路500之1號",
                },
            }
            with patch.object(line_service, "reply_messages", replies), patch.object(line_service, "reply_text", texts):
                await process_webhook_event(session, start)
                await process_webhook_event(session, location)
                self.assertEqual(session.exec(select(AttendanceEvent)).all(), [])
                self.assertIn("U-boss", _pending_clock_site)
                message = replies.await_args.args[1][0]
                self.assertIn("你現在在「金駿76」嗎？", message["text"])
                actions = [item["action"] for item in message["quickReply"]["items"]]
                self.assertIn("action=clocksite:yes", [item.get("data") for item in actions])
                self.assertIn("action=clocksite:assigned", [item.get("data") for item in actions])
                self.assertTrue(all(len(item["label"]) <= 20 for item in actions))
                self.backup_database.assert_not_awaited()
                await process_webhook_event(session, {
                    "type": "postback", "replyToken": "reply-yes",
                    "source": {"type": "user", "userId": "U-boss"},
                    "postback": {"data": "action=clocksite:yes"},
                })
            saved = session.exec(select(AttendanceEvent)).one()
            self.assertEqual(saved.site_id, jinjun.id)
            self.assertEqual(saved.matched_site_id, jinjun.id)
            self.assertAlmostEqual(saved.latitude, JINJUN[0])
            self.assertAlmostEqual(saved.longitude, JINJUN[1])
            self.assertIn("打卡工地與派工不符", saved.note)
            self.assertIn("文化一路", saved.note)
            self.assertNotIn("U-boss", _pending_clock_site)
            self.assertEqual(self.backup_database.await_count, 1)

    def test_admin_can_correct_worksite_without_deleting_attendance(self) -> None:
        with Session(self.engine) as session:
            assigned, jinjun, _blank, owner = self._sites(session)
            worker = Employee(employee_code="EMP001", name="員工", bind_token="ST-EMP", role=Role.employee)
            session.add(worker)
            session.commit()
            event = AttendanceEvent(
                employee_id=owner.id,
                site_id=assigned.id,
                event_type="上班打卡",
                happened_at=datetime.utcnow(),
                note="LINE 位置打卡",
                latitude=JINJUN[0],
                longitude=JINJUN[1],
            )
            session.add(event)
            session.commit()
            before = len(session.exec(select(AttendanceEvent)).all())
            app.dependency_overrides[get_session] = lambda: session
            app.dependency_overrides[get_current_actor] = lambda: owner
            backup = AsyncMock(return_value={"status": "saved"})
            with patch("app.routes.admin.google_drive_worklog_service.backup_database", backup):
                client = TestClient(app)
                denied = client.post(f"/api/attendance/{event.id}/site", json={"worksite_id": jinjun.id, "reason": "   "})
                self.assertEqual(denied.status_code, 400)
                self.assertEqual(denied.json()["detail"], "請填寫修正原因")
                same = client.post(f"/api/attendance/{event.id}/site", json={"worksite_id": assigned.id, "reason": "同一工地"})
                self.assertEqual(same.status_code, 400)
                self.assertEqual(same.json()["detail"], "工地沒有變更")
                app.dependency_overrides[get_current_actor] = lambda: worker
                forbidden = client.post(
                    f"/api/attendance/{event.id}/site",
                    json={"worksite_id": jinjun.id, "reason": "員工不能改"},
                )
                self.assertEqual(forbidden.status_code, 403)
                app.dependency_overrides[get_current_actor] = lambda: owner
                saved = client.post(
                    f"/api/attendance/{event.id}/site",
                    json={"worksite_id": jinjun.id, "reason": "人在金駿76，不是六和機械"},
                )
                client.close()
            self.assertEqual(saved.status_code, 200)
            body = saved.json()
            self.assertEqual(body["site_name"], "金駿76")
            self.assertEqual(body["previous_site_name"], "六和機械")
            self.assertEqual(body["backup_status"], "saved")
            backup.assert_awaited()
            session.refresh(event)
            self.assertEqual(len(session.exec(select(AttendanceEvent)).all()), before)
            self.assertEqual(event.site_id, jinjun.id)
            self.assertAlmostEqual(event.latitude, JINJUN[0])
            self.assertAlmostEqual(event.longitude, JINJUN[1])
            self.assertIn("LINE 位置打卡", event.note)
            self.assertIn("工地已由「六和機械」修正為「金駿76」", event.note)
            revision = session.exec(select(AttendanceSiteRevision)).one()
            self.assertEqual(revision.previous_site_id, assigned.id)
            self.assertEqual(revision.new_site_id, jinjun.id)
            self.assertEqual(revision.reason, "人在金駿76，不是六和機械")
            audit = session.exec(select(AdminAuditLog).where(AdminAuditLog.action == "correct_site")).one()
            self.assertIn("金駿76", audit.summary)
            self.assertNotIn("password", audit.summary.lower())
            rows = build_attendance_rows(session, [owner], date.today())
            self.assertEqual(rows[0]["events"][0]["original_site_name"], "六和機械")
            self.assertEqual(rows[0]["events"][0]["site_name"], "金駿76")

    def test_pending_confirmation_expires(self) -> None:
        from app.services.line import _PendingClockSite, _clock_site_expired

        pending = _PendingClockSite("上班打卡", 1, 2, None, datetime.utcnow() - timedelta(minutes=16))
        self.assertTrue(_clock_site_expired(pending))
        self.assertNotIn("76", spoken_numbers("十六"))
