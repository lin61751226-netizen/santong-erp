from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import AttendanceEvent, Employee
from app.services.google_drive import GoogleDriveWorklogService
from app.services.line import (
    _reply_attendance_options,
    _reply_leave_options,
    line_service,
    process_webhook_event,
    _pending_location_attendance,
)
from app.services.line_platform import LinePlatformError, bind_employee_line_user
from app.services.line_platform import line_platform_service, migrate_santong_rich_menu_links


class LineWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

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
            "message": {"type": "text", "text": "大家今天辛苦了"},
        }
        reply = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch.object(line_service, "reply_text", reply):
            await process_webhook_event(session, event)
        reply.assert_not_awaited()

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
