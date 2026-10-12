# -*- coding: utf-8 -*-
"""群組文字工地歸檔修正測試（2026-10-12 林小咪／林老闆錯歸案例的回歸測試）。

涵蓋：
- GPS 上班打卡（沒點到達工地）即作為工地事實；
- 先發訊息、後到達／後補派工，事後整理可自動歸正；
- 跨工地移動依訊息時間各自歸類；
- 已取消派工不作為依據；
- 重新歸類絕不清空、未綁定記錄原樣保留；
- 人工改工地與稽核軌跡。
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import (
    AdminAuditLog,
    AssignmentMember,
    AttendanceEvent,
    AttendanceEventType,
    Employee,
    GroupTextLog,
    WorkAssignment,
    Worksite,
)
from app.services.group_text_logs import (
    GroupTextReassignError,
    reassign_group_text_log,
    reassign_group_texts_for_date,
)
from app.services.hr import find_assignment_for_employee, resolve_employee_site_at
from app.services.line import process_webhook_event, line_service

# 以 2026-10-12 當地日為準（UTC 邊界 10-11 16:00 ～ 10-12 16:00）。
DAY = date(2026, 10, 12)
CHECK_IN_UTC = datetime(2026, 10, 12, 0, 33)   # 當地 08:33
ARRIVE_UTC = datetime(2026, 10, 12, 0, 51)     # 當地 08:51
TEXT_BEFORE_UTC = datetime(2026, 10, 12, 0, 50)
TEXT_AFTER_UTC = datetime(2026, 10, 12, 1, 28)
AFTERNOON_UTC = datetime(2026, 10, 12, 5, 0)


def _event(employee_id: int, site_id: int, kind: str, at: datetime) -> AttendanceEvent:
    return AttendanceEvent(
        employee_id=employee_id,
        site_id=site_id,
        event_type=kind,
        happened_at=at,
    )


def _group_text(employee_id: int | None, site_id: int | None, at: datetime, msg_id: str, content: str = "工地訊息") -> GroupTextLog:
    return GroupTextLog(
        source_type="line",
        source_id="G1",
        line_user_id=f"U-{employee_id}" if employee_id else "U-stranger",
        employee_id=employee_id,
        site_id=site_id,
        source_message_id=msg_id,
        content=content,
        sent_at=at,
    )


class GroupTextSiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            self.home = Worksite(code="善捷47", name="善捷47")
            self.work = Worksite(code="齊裕53", name="齊裕53")
            self.other = Worksite(code="新竹寶山1", name="新竹寶山1")
            session.add_all([self.home, self.work, self.other])
            session.commit()
            for site in (self.home, self.work, self.other):
                session.refresh(site)
            self.worker = Employee(
                employee_code="EMP005",
                name="林小咪",
                bind_token="TOKEN-MI",
                line_user_id="U-mi",
                home_site_id=self.home.id,
            )
            session.add(self.worker)
            session.commit()
            session.refresh(self.worker)
            self.home_id, self.work_id, self.other_id = self.home.id, self.work.id, self.other.id
            self.worker_id = self.worker.id

    def tearDown(self) -> None:
        self.engine.dispose()

    # --- 解析器 -----------------------------------------------------------

    def test_gps_check_in_counts_as_location_fact(self) -> None:
        """只有 GPS 上班打卡、沒有到達工地事件時，仍應歸到打卡工地。"""
        with Session(self.engine) as session:
            session.add(_event(self.worker_id, self.work_id, AttendanceEventType.check_in.value, CHECK_IN_UTC))
            session.commit()
            site, reason, _ = resolve_employee_site_at(session, self.worker_id, TEXT_AFTER_UTC, DAY)
            self.assertEqual(site.id, self.work_id)
            self.assertEqual(reason, "location_before")

    def test_message_before_arrival_uses_earliest_fact_same_day(self) -> None:
        """到達前幾分鐘的訊息，事後以當日最早位置事實補正。"""
        with Session(self.engine) as session:
            session.add(_event(self.worker_id, self.work_id, AttendanceEventType.arrive_site.value, ARRIVE_UTC))
            session.commit()
            site, reason, _ = resolve_employee_site_at(session, self.worker_id, TEXT_BEFORE_UTC, DAY)
            self.assertEqual(site.id, self.work_id)
            self.assertEqual(reason, "location_after")

    def test_cross_site_movement_picks_latest_fact_before_message(self) -> None:
        with Session(self.engine) as session:
            session.add(_event(self.worker_id, self.home_id, AttendanceEventType.check_in.value, CHECK_IN_UTC))
            session.add(_event(self.worker_id, self.work_id, AttendanceEventType.arrive_site.value, ARRIVE_UTC))
            session.commit()
            morning, _, _ = resolve_employee_site_at(session, self.worker_id, datetime(2026, 10, 12, 0, 40), DAY)
            afternoon, _, _ = resolve_employee_site_at(session, self.worker_id, AFTERNOON_UTC, DAY)
            self.assertEqual(morning.id, self.home_id)
            self.assertEqual(afternoon.id, self.work_id)

    def test_cancelled_assignment_is_ignored(self) -> None:
        with Session(self.engine) as session:
            assignment = WorkAssignment(work_date=DAY, site_id=self.work_id, work_item="移料", status="cancelled")
            session.add(assignment)
            session.commit()
            session.add(AssignmentMember(assignment_id=assignment.id, employee_id=self.worker_id))
            session.commit()
            self.assertIsNone(find_assignment_for_employee(session, self.worker_id, DAY))
            site, reason, _ = resolve_employee_site_at(session, self.worker_id, TEXT_AFTER_UTC, DAY)
            # 無考勤事實、無有效派工 → 退回主工地
            self.assertEqual(site.id, self.home_id)
            self.assertEqual(reason, "home_site")

    def test_assignment_fallback_then_home_site(self) -> None:
        with Session(self.engine) as session:
            site, reason, _ = resolve_employee_site_at(session, self.worker_id, TEXT_AFTER_UTC, DAY)
            self.assertEqual(site.id, self.home_id)
            self.assertEqual(reason, "home_site")

            assignment = WorkAssignment(work_date=DAY, site_id=self.work_id, work_item="移料")
            session.add(assignment)
            session.commit()
            session.add(AssignmentMember(assignment_id=assignment.id, employee_id=self.worker_id))
            session.commit()
            site, reason, resolved_assignment = resolve_employee_site_at(session, self.worker_id, TEXT_AFTER_UTC, DAY)
            self.assertEqual(site.id, self.work_id)
            self.assertEqual(reason, "assignment")
            self.assertEqual(resolved_assignment.id, assignment.id)

    # --- 事後自動重新歸類 --------------------------------------------------

    def test_backfill_moves_home_guess_to_checkin_site(self) -> None:
        """重現林小咪案例：寫入當下只剩主工地可猜，上班卡存在後重新整理即歸正。"""
        with Session(self.engine) as session:
            session.add(_group_text(self.worker_id, self.home_id, TEXT_AFTER_UTC, "m-1", "好的"))
            session.add(_event(self.worker_id, self.work_id, AttendanceEventType.check_in.value, CHECK_IN_UTC))
            session.commit()

            result = reassign_group_texts_for_date(session, DAY)
            self.assertEqual(result["checked"], 1)
            self.assertEqual(result["updated_count"], 1)
            log = session.exec(select(GroupTextLog)).one()
            self.assertEqual(log.site_id, self.work_id)
            self.assertIn("齊裕53", log.note)
            audit = session.exec(select(AdminAuditLog).where(AdminAuditLog.action == "group_text_auto_reassign")).one()
            self.assertIn("善捷47→齊裕53", audit.summary)

    def test_backfill_never_blanks_out_or_touches_unbound(self) -> None:
        with Session(self.engine) as session:
            # 未綁定員工：無法判定，原樣保留
            unbound = _group_text(None, None, TEXT_AFTER_UTC, "m-unbound", "陌生人發言")
            # 已歸在正確工地：不動
            correct = _group_text(self.worker_id, self.work_id, TEXT_AFTER_UTC, "m-ok", "一明偉下貨骨料")
            session.add_all([unbound, correct])
            session.add(_event(self.worker_id, self.work_id, AttendanceEventType.check_in.value, CHECK_IN_UTC))
            session.commit()

            result = reassign_group_texts_for_date(session, DAY)
            self.assertEqual(result["updated_count"], 0)
            self.assertIn(unbound.id, result["unresolved_ids"])
            rows = session.exec(select(GroupTextLog).order_by(GroupTextLog.id)).all()
            self.assertIsNone(rows[0].site_id)
            self.assertEqual(rows[1].site_id, self.work_id)
            self.assertEqual(session.exec(select(AdminAuditLog)).all(), [])

    def test_backfill_picks_up_assignment_created_after_message(self) -> None:
        with Session(self.engine) as session:
            session.add(_group_text(self.worker_id, self.home_id, TEXT_AFTER_UTC, "m-late", "粗工叫好了"))
            session.commit()
            # 派工單在訊息之後才補建
            assignment = WorkAssignment(work_date=DAY, site_id=self.work_id, work_item="堆高機移料")
            session.add(assignment)
            session.commit()
            session.add(AssignmentMember(assignment_id=assignment.id, employee_id=self.worker_id))
            session.commit()

            result = reassign_group_texts_for_date(session, DAY)
            self.assertEqual(result["updated_count"], 1)
            log = session.exec(select(GroupTextLog)).one()
            self.assertEqual(log.site_id, self.work_id)
            self.assertEqual(log.assignment_id, assignment.id)

    def test_manual_reassign_with_audit_and_invalid_site(self) -> None:
        boss = Employee(employee_code="BOSS001", name="林老闆", bind_token="TOKEN-BOSS", line_user_id="U-boss")
        with Session(self.engine) as session:
            session.add(boss)
            log = _group_text(None, self.other_id, TEXT_BEFORE_UTC, "m-boss", "10/15 40尺貨櫃")
            # 人工補綁員工後再改工地（模擬老闆那筆無考勤事實的紀錄）
            log.employee_id = None
            session.add(log)
            session.commit()
            log_id = log.id

            moved = reassign_group_text_log(session, log_id, self.work_id, boss)
            self.assertEqual(moved.site_id, self.work_id)
            self.assertIn("人工改歸", moved.note)
            audit = session.exec(select(AdminAuditLog).where(AdminAuditLog.action == "group_text_manual_reassign")).one()
            self.assertEqual(audit.entity_id, log_id)

            with self.assertRaises(GroupTextReassignError):
                reassign_group_text_log(session, 99999, self.work_id, boss)

    def test_manual_site_is_locked_against_auto_heal(self) -> None:
        with Session(self.engine) as session:
            boss = Employee(
                employee_code="BOSS001", name="林老闆", bind_token="TOKEN-BOSS",
                line_user_id="U-boss", home_site_id=self.other_id,
            )
            session.add(boss)
            session.commit()
            session.refresh(boss)
            log = _group_text(boss.id, self.other_id, TEXT_BEFORE_UTC, "m-boss", "40尺貨櫃")
            session.add(log)
            session.commit()
            # 人工改歸齊裕53並鎖定
            reassign_group_text_log(session, log.id, self.work_id, boss)
            # 之後即使出現齊裕53以外的考勤事實（誤打在主工地），自動歸類也不得覆蓋
            session.add(_event(boss.id, self.other_id, AttendanceEventType.check_in.value, ARRIVE_UTC))
            session.commit()
            result = reassign_group_texts_for_date(session, DAY)
            self.assertEqual(result["updated_count"], 0)
            stored = session.get(GroupTextLog, log.id)
            self.assertEqual(stored.site_id, self.work_id)
            self.assertTrue(stored.site_manual)


class GroupTextWritePathTests(unittest.IsolatedAsyncioTestCase):
    """即時寫入路徑（LINE webhook）的整合驗證。"""

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
        self.backup_patcher.start()
        with Session(self.engine) as session:
            self.home = Worksite(code="善捷47", name="善捷47")
            self.work = Worksite(code="齊裕53", name="齊裕53")
            session.add_all([self.home, self.work])
            session.commit()
            session.refresh(self.home)
            session.refresh(self.work)
            self.worker = Employee(
                employee_code="EMP005", name="林小咪", bind_token="TOKEN-MI",
                line_user_id="U-mi", home_site_id=self.home.id,
            )
            session.add(self.worker)
            session.commit()
            session.refresh(self.worker)
            self.work_id = self.work.id
            self.worker_id = self.worker.id

    def tearDown(self) -> None:
        self.backup_patcher.stop()
        self.engine.dispose()

    async def test_group_text_after_gps_checkin_goes_to_checkin_site(self) -> None:
        """重現林小咪 10/12：08:33 GPS 上班打卡在齊裕53，09:28 訊息不得落到善捷47。"""
        with Session(self.engine) as session:
            session.add(AttendanceEvent(
                employee_id=self.worker_id, site_id=self.work_id,
                event_type=AttendanceEventType.check_in.value, happened_at=CHECK_IN_UTC,
            ))
            session.commit()
            event_time = TEXT_AFTER_UTC.replace(tzinfo=timezone.utc)
            with patch.object(line_service, "reply_text", AsyncMock()):
                await process_webhook_event(session, {
                    "type": "message",
                    "timestamp": int(event_time.timestamp() * 1000),
                    "replyToken": "reply-token",
                    "source": {"type": "group", "groupId": "G1", "userId": "U-mi"},
                    "message": {"id": "mi-message", "type": "text", "text": "好的"},
                })
            saved = session.exec(select(GroupTextLog)).one()
            self.assertEqual(saved.site_id, self.work_id)


if __name__ == "__main__":
    unittest.main()
