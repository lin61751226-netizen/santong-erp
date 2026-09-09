from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.pool import StaticPool
from sqlmodel import select
from sqlmodel import Session, SQLModel, create_engine

from app.models import (
    AssignmentMember, AttendanceEvent, AttendanceEventType, Employee, GroupTextLog,
    PhotoUploadLog, WorkAssignment, WorkReportEvent, Worksite,
)
from app.services.google_drive import GoogleDriveWorklogService
from app.services.line import (
    _reply_attendance_options,
    _reply_leave_options,
    _handle_image_message,
    line_service,
    process_webhook_event,
    _pending_location_attendance,
)
from app.services.line_platform import LinePlatformError, bind_employee_line_user
from app.services.line_platform import line_platform_service, migrate_santong_rich_menu_links
from app.services.google_drive import DriveUploadResult


class LineWorkflowTests(unittest.IsolatedAsyncioTestCase):
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

    def tearDown(self) -> None:
        self.backup_patcher.stop()
        self.engine.dispose()

    async def test_attendance_menu_contains_check_in_and_check_out(self) -> None:
        reply = AsyncMock(return_value=(True, "sent"))
        with patch.object(line_service, "reply_messages", reply):
            await _reply_attendance_options("reply-token")

        messages = reply.await_args.args[1]
        actions = messages[0]["quickReply"]["items"]
        self.assertEqual(
            [item["action"]["label"] for item in actions],
            ["上班打卡", "下班打卡"],
        )

    async def test_attendance_check_in_requires_location_and_saves_coordinates(self) -> None:
        with Session(self.engine) as session:
            employee = Employee(
                employee_code="EMP-LOC",
                name="定位員工",
                bind_token="TOKEN-LOC",
                line_user_id="U-loc",
            )
            session.add(employee)
            session.commit()
            reply = AsyncMock(return_value=(True, "sent"))
            start_event = {
                "type": "message", "replyToken": "reply-start",
                "source": {"type": "user", "userId": "U-loc"},
                "message": {"type": "text", "text": "上班打卡"},
            }
            location_event = {
                "type": "message", "replyToken": "reply-location",
                "source": {"type": "user", "userId": "U-loc"},
                "message": {"type": "location", "latitude": 25.033964, "longitude": 121.564468, "address": "台北市"},
            }
            with patch.object(line_service, "reply_messages", reply):
                await process_webhook_event(session, start_event)
                self.assertEqual(_pending_location_attendance["U-loc"], "上班打卡")
                await process_webhook_event(session, location_event)

            saved = session.query(AttendanceEvent).filter(AttendanceEvent.employee_id == employee.id).one()
            self.assertEqual(saved.event_type, "上班打卡")
            self.assertAlmostEqual(saved.latitude, 25.033964)
            self.assertAlmostEqual(saved.longitude, 121.564468)
            self.assertNotIn("U-loc", _pending_location_attendance)
            self.assertEqual(self.backup_database.await_count, 1)

    async def test_photo_uses_latest_arrival_site_for_drive_folder_and_log(self) -> None:
        with Session(self.engine) as session:
            employee = Employee(employee_code="EMP-PHOTO", name="拍照員工", bind_token="TOKEN-PHOTO", line_user_id="U-photo")
            site = Worksite(code="SITE-PHOTO", name="照片工地")
            session.add_all([employee, site])
            session.commit()
            session.add(AttendanceEvent(employee_id=employee.id, site_id=site.id,
                                        event_type=AttendanceEventType.arrive_site.value))
            session.commit()
            upload = DriveUploadResult("file-1", "photo.jpg", "https://drive/photo", "folder-1", "2026-09-06_照片工地", "image/jpeg")
            with patch.object(line_platform_service, "get_message_content", AsyncMock(return_value=(b"img", "image/jpeg"))), \
                 patch.object(line_service, "reply_text", AsyncMock()), \
                 patch.object(line_service, "reply_messages", AsyncMock()), \
                 patch("app.services.line.google_drive_worklog_service.upload_line_photo", AsyncMock(return_value=upload)) as upload_photo:
                await _handle_image_message(
                    session, event={"timestamp": 0},
                    message={"id": "message-1"}, reply_token="reply", line_user_id="U-photo", employee=employee,
                )
            self.assertEqual(upload_photo.await_args.kwargs["site_name"], "照片工地")
            saved = session.exec(select(PhotoUploadLog)).one()
            self.assertEqual(saved.site_id, site.id)
            self.assertEqual(self.backup_database.await_count, 1)

    async def test_shanjie_47_group_photo_uses_group_site_even_when_uploader_is_unbound(self) -> None:
        with Session(self.engine) as session:
            site = Worksite(code="善捷47", name="善捷47")
            session.add(site)
            session.commit()
            session.refresh(site)
            upload = DriveUploadResult(
                "file-47", "photo.jpg", "https://drive/photo-47", "folder-47",
                "2026-09-09_善捷47", "image/jpeg",
            )
            event = {
                "timestamp": 0,
                "source": {"type": "group", "groupId": "G-shanjie-47", "userId": "U-unbound"},
            }
            with (
                patch.object(line_platform_service, "get_group_display_name", AsyncMock(return_value="善捷47工作群組")),
                patch.object(line_platform_service, "get_source_display_name", AsyncMock(return_value="未綁定員工")),
                patch("app.services.line.google_drive_worklog_service.upload_line_photo", AsyncMock(return_value=upload)) as upload_photo,
            ):
                await _handle_image_message(
                    session,
                    event=event,
                    message={"id": "group-photo-47"},
                    reply_token="reply",
                    line_user_id="U-unbound",
                    employee=None,
                )

            self.assertEqual(upload_photo.await_args.kwargs["site_name"], "善捷47")
            saved = session.exec(select(PhotoUploadLog)).one()
            self.assertEqual(saved.site_id, site.id)
            self.assertIn("群組工地：善捷47", saved.note)
            self.assertEqual(self.backup_database.await_count, 1)

    async def test_group_display_name_reads_line_group_summary(self) -> None:
        source = {"type": "group", "groupId": "G-shanjie-47", "userId": "U1"}
        with patch.object(
            line_platform_service,
            "_request",
            AsyncMock(return_value={"groupName": "善捷47"}),
        ) as request:
            group_name = await line_platform_service.get_group_display_name(source)

        self.assertEqual(group_name, "善捷47")
        self.assertIn("/group/G-shanjie-47/summary", request.await_args.args[1])

    async def test_work_report_is_saved_as_history_without_overwriting_previous_report(self) -> None:
        with Session(self.engine) as session:
            employee = Employee(
                employee_code="EMP-REPORT",
                name="回報員工",
                bind_token="TOKEN-REPORT",
                line_user_id="U-report",
            )
            session.add(employee)
            session.commit()
            reply = AsyncMock(return_value=(True, "sent"))
            with patch.object(line_service, "reply_text", reply):
                for text in ("工作開始", "工作完成"):
                    await process_webhook_event(session, {
                        "type": "message", "replyToken": "reply-report",
                        "source": {"type": "user", "userId": "U-report"},
                        "message": {"type": "text", "text": text},
                    })

            reports = session.exec(
                select(WorkReportEvent).where(WorkReportEvent.employee_id == employee.id)
            ).all()
            self.assertEqual([report.event_type for report in reports], ["工作開始", "工作完成"])
            with Session(self.engine) as reopened:
                saved = reopened.exec(select(WorkReportEvent)).all()
                self.assertEqual(len(saved), 2)
            self.assertEqual(self.backup_database.await_count, 2)

    async def test_leave_menu_contains_all_leave_types(self) -> None:
        reply = AsyncMock(return_value=(True, "sent"))
        with patch.object(line_service, "reply_messages", reply):
            await _reply_leave_options("reply-token")

        messages = reply.await_args.args[1]
        labels = [item["action"]["label"] for item in messages[0]["quickReply"]["items"]]
        self.assertEqual(labels, ["事假", "病假", "特休", "公假", "排休", "其他"])

    async def test_normal_group_text_is_silent(self) -> None:
        event = {
            "type": "message",
            "replyToken": "reply-token",
            "source": {"type": "group", "groupId": "G1", "userId": "U1"},
            "message": {"id": "group-message-1", "type": "text", "text": "大家今天辛苦了"},
        }
        reply = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch.object(line_service, "reply_text", reply):
            await process_webhook_event(session, event)
            saved = session.exec(select(GroupTextLog)).one()
            self.assertEqual(saved.source_id, "G1")
            self.assertEqual(saved.content, "大家今天辛苦了")
            self.assertEqual(saved.review_status, "pending")
        reply.assert_not_awaited()
        self.backup_database.assert_awaited_once()

    async def test_group_text_redelivery_is_saved_once(self) -> None:
        event = {
            "type": "message",
            "replyToken": "reply-token",
            "source": {"type": "group", "groupId": "G1", "userId": "U1"},
            "message": {"id": "same-message", "type": "text", "text": "同一則訊息"},
        }
        with Session(self.engine) as session, patch.object(line_service, "reply_text", AsyncMock()):
            await process_webhook_event(session, event)
            await process_webhook_event(session, event)
            self.assertEqual(len(session.exec(select(GroupTextLog)).all()), 1)
        self.backup_database.assert_awaited_once()

    async def test_group_text_uses_daily_assignment_site(self) -> None:
        with Session(self.engine) as session:
            employee = Employee(
                employee_code="EMP-GROUP", name="群組員工", bind_token="TOKEN-GROUP",
                line_user_id="U-group",
            )
            site = Worksite(code="GROUP-SITE", name="群組工地")
            session.add_all([employee, site])
            session.commit()
            assignment = WorkAssignment(work_date=date(2026, 9, 8), site_id=site.id, work_item="搬運")
            session.add(assignment)
            session.commit()
            session.add(AssignmentMember(assignment_id=assignment.id, employee_id=employee.id))
            session.commit()
            event_time = datetime(2026, 9, 8, 1, tzinfo=timezone.utc)
            await process_webhook_event(session, {
                "type": "message",
                "timestamp": int(event_time.timestamp() * 1000),
                "replyToken": "reply-token",
                "source": {"type": "group", "groupId": "G1", "userId": "U-group"},
                "message": {"id": "assigned-message", "type": "text", "text": "已完成卸料"},
            })
            saved = session.exec(select(GroupTextLog)).one()
            self.assertEqual(saved.employee_id, employee.id)
            self.assertEqual(saved.site_id, site.id)
            self.assertEqual(saved.assignment_id, assignment.id)

    async def test_menu_switch_postback_is_silent(self) -> None:
        event = {
            "type": "postback",
            "replyToken": "reply-token",
            "source": {"type": "group", "groupId": "G1", "userId": "U1"},
            "postback": {"data": "action=menu:switch-main"},
        }
        reply = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch.object(line_service, "reply_text", reply):
            await process_webhook_event(session, event)
        reply.assert_not_awaited()

    def test_existing_binding_cannot_be_overwritten(self) -> None:
        with Session(self.engine) as session:
            employee = Employee(
                employee_code="EMP001",
                name="員工一",
                bind_token="TOKEN001",
                line_user_id="U-existing",
            )
            session.add(employee)
            session.commit()
            session.refresh(employee)

            with self.assertRaises(LinePlatformError):
                bind_employee_line_user(session, employee, "U-different")

            session.refresh(employee)
            self.assertEqual(employee.line_user_id, "U-existing")

    async def test_drive_restore_only_fills_empty_bindings(self) -> None:
        with Session(self.engine) as session:
            first = Employee(
                employee_code="EMP001",
                name="員工一",
                bind_token="TOKEN001",
            )
            second = Employee(
                employee_code="EMP002",
                name="員工二",
                bind_token="TOKEN002",
                line_user_id="U-current",
            )
            session.add(first)
            session.add(second)
            session.commit()

            service = GoogleDriveWorklogService()
            document = {
                "version": 1,
                "bindings": [
                    {"employee_code": "EMP001", "line_user_id": "U-restored"},
                    {"employee_code": "EMP002", "line_user_id": "U-old"},
                ],
            }
            with (
                patch.object(service, "is_configured", return_value=True),
                patch.object(service, "_load_line_bindings_document", return_value=document),
            ):
                result = await service.restore_line_bindings(session)

            self.assertEqual(result["restored_employee_codes"], ["EMP001"])
            self.assertEqual(session.get(Employee, first.id).line_user_id, "U-restored")
            self.assertEqual(session.get(Employee, second.id).line_user_id, "U-current")

    async def test_rich_menu_migration_replaces_all_previous_menus(self) -> None:
        existing_menus = [
            {"name": "santong-main", "richMenuId": "richmenu-old-main-1"},
            {"name": "santong-main", "richMenuId": "richmenu-current-main"},
            {"name": "santong-tools", "richMenuId": "richmenu-old-tools-1"},
            {"name": "other-menu", "richMenuId": "richmenu-other"},
        ]
        validate = AsyncMock()
        replace = AsyncMock()
        with (
            patch.object(line_platform_service, "validate_rich_menu_batch", validate),
            patch.object(line_platform_service, "replace_rich_menu_links", replace),
        ):
            result = await migrate_santong_rich_menu_links(
                existing_menus=existing_menus,
                main_rich_menu_id="richmenu-current-main",
                tools_rich_menu_id="richmenu-current-tools",
            )

        self.assertEqual(result["operation_count"], 2)
        operations = validate.await_args.args[0]
        self.assertEqual(
            operations,
            [
                {
                    "type": "link",
                    "from": "richmenu-old-main-1",
                    "to": "richmenu-current-main",
                },
                {
                    "type": "link",
                    "from": "richmenu-old-tools-1",
                    "to": "richmenu-current-tools",
                },
            ],
        )
        replace.assert_awaited_once_with(operations, validate.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
