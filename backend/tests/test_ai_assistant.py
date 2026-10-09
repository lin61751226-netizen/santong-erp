from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.config import settings
from app.models import (
    AdminAuditLog,
    AiInteractionLog,
    AssignmentMember,
    AttendanceEvent,
    Employee,
    EmployeeStatus,
    LeaveRequest,
    LeaveStatus,
    Role,
    WorkAssignment,
    WorkReportEvent,
    Worksite,
)
from app.services.ai_assistant import FRIENDLY_FAILURE_TEXT, ParsedIntent, parse_user_text
from app.services.line import _pending_location_attendance, line_service, process_webhook_event


def intent(name: str, **overrides) -> ParsedIntent:
    payload = dict(
        intent=name,
        needs_clarification=False,
        clarification_question=None,
        work_date=None,
        end_date=None,
        worksite_text=None,
        employee_names=[],
        leave_type=None,
        reason=None,
        equipment_text=None,
        equipment_count=None,
        work_item=None,
        report_note=None,
        query_employee_name=None,
        is_completion=False,
    )
    payload.update(overrides)
    payload["intent"] = name
    return ParsedIntent(**payload)


def combined_reply(reply_text: AsyncMock, reply_messages: AsyncMock) -> str:
    chunks: list[str] = []
    if reply_text.await_count:
        chunks.append(str(reply_text.await_args.args[1]))
    if reply_messages.await_count:
        message = reply_messages.await_args.args[1][0]
        chunks.append(message["text"])
        for item in message.get("quickReply", {}).get("items", []):
            action = item["action"]
            chunks.append(str(action.get("label") or ""))
            chunks.append(str(action.get("data") or action.get("text") or ""))
    return "\n".join(chunks)


class AiAssistantTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.backup = patch(
            "app.services.line.google_drive_worklog_service.backup_database",
            new=AsyncMock(return_value={"status": "saved"}),
        )
        self.backup.start()
        self.enabled = patch.object(settings, "ai_assistant_enabled", True)
        self.enabled.start()
        self.key = patch.object(settings, "openai_api_key", "sk-test-secret")
        self.key.start()
        self.today = patch("app.services.ai_assistant.local_today", return_value=date(2026, 10, 9))
        self.today.start()

    def tearDown(self) -> None:
        _pending_location_attendance.clear()
        self.today.stop()
        self.key.stop()
        self.enabled.stop()
        self.backup.stop()
        self.engine.dispose()

    def _people(self, session: Session) -> dict[str, Employee | Worksite]:
        site = Worksite(code="善捷47", name="善捷47", is_active=True)
        other = Worksite(code="齊裕53", name="齊裕53", is_active=True)
        boss = Employee(
            employee_code="BOSS001", name="三通工程行林老闆", bind_token="ST-BOSS",
            role=Role.owner, line_user_id="U-boss",
        )
        sheng = Employee(employee_code="EMP001", name="勝忠", bind_token="ST-WIN", role=Role.employee, line_user_id="U-win")
        ray = Employee(
            employee_code="EMP003", name="建成Ray Rostova", bind_token="ST-RAY",
            role=Role.employee, line_user_id="U-ray",
        )
        session.add_all([site, other, boss, sheng, ray])
        session.commit()
        return {"site": site, "other": other, "boss": boss, "sheng": sheng, "ray": ray}

    async def _send(self, session: Session, user_id: str, text: str, reply_text, reply_messages, *, postback: str | None = None):
        if postback is None:
            event = {
                "type": "message",
                "replyToken": "reply-token",
                "source": {"type": "user", "userId": user_id},
                "message": {"type": "text", "text": text},
            }
        else:
            event = {
                "type": "postback",
                "replyToken": "reply-token",
                "source": {"type": "user", "userId": user_id},
                "postback": {"data": postback},
            }
        with patch.object(line_service, "reply_text", reply_text), patch.object(line_service, "reply_messages", reply_messages):
            await process_webhook_event(session, event)

    async def test_assignment_confirm_then_save_uses_existing_records(self) -> None:
        parsed = intent(
            "assignment",
            work_date="2026-10-10",
            worksite_text="47標",
            employee_names=["勝忠", "建成"],
            equipment_text="堆高機",
            equipment_count=2,
        )
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ) as parser:
            people = self._people(session)
            await self._send(session, "U-boss", "明天 47 標要兩台堆高機，勝忠跟建成去", reply_text, reply_messages)
            shown = combined_reply(reply_text, reply_messages)
            self.assertIn("善捷47", shown)
            self.assertIn("勝忠", shown)
            self.assertIn("建成Ray Rostova", shown)
            self.assertIn("堆高機 2 台", shown)
            self.assertIn("請確認派工", shown)
            self.assertNotIn("我聽到", shown)
            self.assertEqual(len(session.exec(select(WorkAssignment)).all()), 0)
            data = reply_messages.await_args.args[1][0]["quickReply"]["items"][0]["action"]["data"]
            self.assertTrue(data.startswith("action=ai:confirm:"))
            reply_messages.reset_mock()
            reply_text.reset_mock()
            await self._send(session, "U-boss", "", reply_text, reply_messages, postback=data)
            self.assertEqual(parser.await_count, 1)
            saved = session.exec(select(WorkAssignment)).one()
            self.assertEqual(saved.site_id, people["site"].id)
            self.assertEqual(saved.work_date, date(2026, 10, 10))
            self.assertEqual(saved.work_item, "堆高機作業")
            self.assertIn("堆高機 2 台", saved.equipment or "")
            member_ids = {
                row.employee_id
                for row in session.exec(select(AssignmentMember).where(AssignmentMember.assignment_id == saved.id)).all()
            }
            self.assertEqual(member_ids, {people["sheng"].id, people["ray"].id})
            self.assertTrue(session.exec(select(AdminAuditLog).where(AdminAuditLog.entity_type == "work_assignment")).first())
            logs = session.exec(select(AiInteractionLog)).all()
            self.assertIn("saved", {row.outcome for row in logs})
            blob = json.dumps([row.model_dump() for row in logs], ensure_ascii=False, default=str)
            self.assertNotIn("sk-test-secret", blob)
            reply_messages.reset_mock()
            reply_text.reset_mock()
            await self._send(session, "U-boss", "", reply_text, reply_messages, postback=data)
            self.assertIn("已經處理過", combined_reply(reply_text, reply_messages))
            self.assertEqual(len(session.exec(select(WorkAssignment)).all()), 1)

    async def test_cancel_does_not_save(self) -> None:
        parsed = intent(
            "assignment", work_date="2026-10-10", worksite_text="善捷47",
            employee_names=["勝忠"], work_item="物料搬運",
        )
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            self._people(session)
            await self._send(session, "U-boss", "派勝忠去善捷", reply_text, reply_messages)
            data = reply_messages.await_args.args[1][0]["quickReply"]["items"][1]["action"]["data"]
            reply_text.reset_mock()
            reply_messages.reset_mock()
            await self._send(session, "U-boss", "", reply_text, reply_messages, postback=data)
            self.assertIn("已取消", combined_reply(reply_text, reply_messages))
            self.assertEqual(len(session.exec(select(WorkAssignment)).all()), 0)
            self.assertEqual(
                session.exec(select(AiInteractionLog).where(AiInteractionLog.outcome == "cancelled")).one().outcome,
                "cancelled",
            )

    async def test_ambiguous_worksite_asks_before_save(self) -> None:
        parsed = intent(
            "assignment", work_date="2026-10-10", worksite_text="寶山",
            employee_names=["勝忠"], work_item="物料搬運",
        )
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            people = self._people(session)
            first = Worksite(code="新竹寶山1", name="新竹寶山1")
            second = Worksite(code="新竹寶山2", name="新竹寶山2")
            session.add_all([first, second])
            session.commit()
            await self._send(session, "U-boss", "明天寶山派勝忠", reply_text, reply_messages)
            shown = combined_reply(reply_text, reply_messages)
            self.assertIn("請點選", shown)
            self.assertEqual(len(session.exec(select(WorkAssignment)).all()), 0)
            buttons = reply_messages.await_args.args[1][0]["quickReply"]["items"]
            target = next(item for item in buttons if item["action"]["label"] == "新竹寶山2")
            reply_messages.reset_mock()
            await self._send(session, "U-boss", "", reply_text, reply_messages, postback=target["action"]["data"])
            confirmed = combined_reply(reply_text, reply_messages)
            self.assertIn("新竹寶山2", confirmed)
            self.assertIn("請確認派工", confirmed)
            self.assertNotIn(str(people["site"].name), confirmed.split("請確認派工", 1)[-1])

    async def test_employee_cannot_create_assignment(self) -> None:
        parsed = intent(
            "assignment", work_date="2026-10-10", worksite_text="47標",
            employee_names=["建成"], equipment_text="堆高機", equipment_count=1,
        )
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            self._people(session)
            await self._send(session, "U-win", "明天派建成去47標", reply_text, reply_messages)
            self.assertIn("不能建立派工", combined_reply(reply_text, reply_messages))
            self.assertEqual(len(session.exec(select(WorkAssignment)).all()), 0)
            self.assertEqual(session.exec(select(AiInteractionLog)).one().outcome, "denied")

    async def test_site_manager_cannot_assign_outside_home_site(self) -> None:
        parsed = intent(
            "assignment", work_date="2026-10-10", worksite_text="齊裕53",
            employee_names=["勝忠"], work_item="物料搬運",
        )
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            people = self._people(session)
            manager = Employee(
                employee_code="SITE001", name="工地主管", bind_token="ST-SITE",
                role=Role.site_manager, line_user_id="U-site", home_site_id=people["site"].id,
            )
            people["sheng"].home_site_id = people["site"].id
            session.add(manager)
            session.add(people["sheng"])
            session.commit()
            await self._send(session, "U-site", "明天齊裕53派勝忠", reply_text, reply_messages)
            self.assertIn("不在你能派工的範圍", combined_reply(reply_text, reply_messages))
            self.assertEqual(len(session.exec(select(WorkAssignment)).all()), 0)

    async def test_leave_confirm_saves_and_roster_limit_blocks(self) -> None:
        ok = intent("leave", work_date="2026-10-12", leave_type="排休", reason="想排休", employee_names=["我"])
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=ok),
        ):
            people = self._people(session)
            await self._send(session, "U-win", "我下週一想排休", reply_text, reply_messages)
            self.assertIn("請確認請假", combined_reply(reply_text, reply_messages))
            self.assertEqual(len(session.exec(select(LeaveRequest)).all()), 0)
            data = reply_messages.await_args.args[1][0]["quickReply"]["items"][0]["action"]["data"]
            reply_text.reset_mock()
            reply_messages.reset_mock()
            await self._send(session, "U-win", "", reply_text, reply_messages, postback=data)
            saved = session.exec(select(LeaveRequest)).one()
            self.assertEqual(saved.employee_id, people["sheng"].id)
            self.assertEqual(saved.leave_type, "排休")
            self.assertEqual(saved.status, LeaveStatus.pending)
            self.assertEqual(saved.start_date, date(2026, 10, 12))

            blocked = intent("leave", work_date="2026-10-20", leave_type="排休", reason="再排一天", employee_names=["我"])
            session.add(LeaveRequest(
                employee_id=people["sheng"].id, leave_type="排休",
                start_date=date(2026, 10, 1), end_date=date(2026, 10, 6),
                reason="已排", status=LeaveStatus.approved,
            ))
            session.commit()
            with patch("app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=blocked)):
                reply_text.reset_mock()
                reply_messages.reset_mock()
                await self._send(session, "U-win", "10/20 再排休", reply_text, reply_messages)
            self.assertIn("六天", combined_reply(reply_text, reply_messages))
            self.assertEqual(len(session.exec(select(LeaveRequest)).all()), 2)

    async def test_cross_month_roster_leave_is_rejected(self) -> None:
        parsed = intent(
            "leave", work_date="2026-10-30", end_date="2026-11-02",
            leave_type="排休", reason="跨月", employee_names=["我"],
        )
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            self._people(session)
            await self._send(session, "U-win", "10/30 到 11/2 排休", reply_text, reply_messages)
            self.assertIn("不可跨月", combined_reply(reply_text, reply_messages))
            self.assertEqual(len(session.exec(select(LeaveRequest)).all()), 0)

    async def test_unknown_employee_is_not_created(self) -> None:
        parsed = intent(
            "assignment", work_date="2026-10-10", worksite_text="善捷47",
            employee_names=["王大明"], work_item="物料搬運",
        )
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            self._people(session)
            before = len(session.exec(select(Employee)).all())
            await self._send(session, "U-boss", "明天派王大明去善捷", reply_text, reply_messages)
            self.assertIn("找不到員工", combined_reply(reply_text, reply_messages))
            self.assertEqual(len(session.exec(select(Employee)).all()), before)
            self.assertEqual(len(session.exec(select(WorkAssignment)).all()), 0)

    async def test_work_report_confirm_then_save(self) -> None:
        parsed = intent(
            "work_report",
            worksite_text="善捷工地",
            report_note="今天善捷工地做完了，堆高機搬了三車鋼筋",
            is_completion=True,
        )
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            people = self._people(session)
            await self._send(session, "U-win", "今天善捷工地做完了，堆高機搬了三車鋼筋", reply_text, reply_messages)
            self.assertIn("工作完成", combined_reply(reply_text, reply_messages))
            self.assertEqual(len(session.exec(select(WorkReportEvent)).all()), 0)
            data = reply_messages.await_args.args[1][0]["quickReply"]["items"][0]["action"]["data"]
            await self._send(session, "U-win", "", reply_text, reply_messages, postback=data)
            saved = session.exec(select(WorkReportEvent)).one()
            self.assertEqual(saved.employee_id, people["sheng"].id)
            self.assertEqual(saved.event_type, "工作完成")
            self.assertEqual(saved.site_id, people["site"].id)
            self.assertIn("三車鋼筋", saved.note or "")

    async def test_query_schedule_reads_database(self) -> None:
        parsed = intent("query_schedule", work_date="2026-10-10", query_employee_name="我")
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            people = self._people(session)
            assignment = WorkAssignment(work_date=date(2026, 10, 10), site_id=people["site"].id, work_item="堆高機移料")
            session.add(assignment)
            session.commit()
            session.add(AssignmentMember(assignment_id=assignment.id, employee_id=people["sheng"].id))
            session.commit()
            await self._send(session, "U-win", "明天我去哪個工地？", reply_text, reply_messages)
            shown = combined_reply(reply_text, reply_messages)
            self.assertIn("善捷47", shown)
            self.assertIn("堆高機移料", shown)
            self.assertEqual(len(session.exec(select(WorkAssignment)).all()), 1)

    async def test_clock_in_uses_existing_location_flow(self) -> None:
        parsed = intent("clock_in")
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            self._people(session)
            await self._send(session, "U-win", "上班", reply_text, reply_messages)
            self.assertEqual(_pending_location_attendance["U-win"], "上班打卡")
            self.assertIn("傳送目前位置", combined_reply(reply_text, reply_messages))
            self.assertEqual(len(session.exec(select(AttendanceEvent)).all()), 0)

    async def test_arrive_without_assignment_reuses_site_picker(self) -> None:
        parsed = intent("arrive_site")
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            people = self._people(session)
            await self._send(session, "U-win", "我到工地了", reply_text, reply_messages)
            shown = combined_reply(reply_text, reply_messages)
            self.assertIn(f"到達工地:{people['site'].id}", shown)
            self.assertEqual(len(session.exec(select(AttendanceEvent)).all()), 0)

    async def test_api_failure_falls_back_without_crashing(self) -> None:
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))

        async def explode(*args, **kwargs):
            raise RuntimeError("timeout sk-test-secret")

        with Session(self.engine) as session, patch("app.services.ai_assistant.parse_user_text", new=explode):
            self._people(session)
            await self._send(session, "U-win", "明天我想請假", reply_text, reply_messages)
            self.assertIn(FRIENDLY_FAILURE_TEXT, combined_reply(reply_text, reply_messages))
            self.assertEqual(len(session.exec(select(LeaveRequest)).all()), 0)
            log = session.exec(select(AiInteractionLog)).one()
            self.assertEqual(log.outcome, "fallback")
            self.assertEqual(log.detail, "RuntimeError")
            self.assertNotIn("sk-test-secret", log.detail or "")
            self.assertNotIn("sk-test-secret", log.parsed_intent or "")

    async def test_missing_key_does_not_call_openai(self) -> None:
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))

        def forbid_client(*args, **kwargs):
            raise AssertionError("不應建立 OpenAI 客戶端")

        with Session(self.engine) as session, patch.object(settings, "openai_api_key", "  "), patch(
            "openai.AsyncOpenAI", forbid_client,
        ):
            self._people(session)
            await self._send(session, "U-win", "明天我想請假", reply_text, reply_messages)
            self.assertIn(FRIENDLY_FAILURE_TEXT, combined_reply(reply_text, reply_messages))

    async def test_disabled_flag_keeps_the_original_unknown_reply(self) -> None:
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))

        async def explode(*args, **kwargs):
            raise AssertionError("功能關閉時不應解析")

        with Session(self.engine) as session, patch.object(settings, "ai_assistant_enabled", False), patch(
            "app.services.ai_assistant.parse_user_text", new=explode,
        ):
            self._people(session)
            await self._send(session, "U-win", "明天我想請假", reply_text, reply_messages)
            self.assertEqual(
                reply_text.await_args.args[1],
                "沒看懂這個指令，可直接點下方 Rich Menu 按鈕，或輸入「指令」查看可用功能。",
            )
            reply_messages.assert_not_awaited()
            self.assertEqual(len(session.exec(select(AiInteractionLog)).all()), 0)

    async def test_existing_commands_do_not_call_the_parser(self) -> None:
        async def explode(*args, **kwargs):
            raise AssertionError("既有指令不應呼叫自然語言解析")

        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch("app.services.ai_assistant.parse_user_text", new=explode):
            self._people(session)
            await self._send(session, "U-win", "上班打卡", reply_text, reply_messages)
            self.assertEqual(_pending_location_attendance["U-win"], "上班打卡")
            reply_text.reset_mock()
            reply_messages.reset_mock()
            await self._send(session, "U-win", "我的行程", reply_text, reply_messages)
            self.assertIn("今天沒有排定工作", combined_reply(reply_text, reply_messages))
            reply_text.reset_mock()
            await self._send(session, "U-win", "指令", reply_text, reply_messages)
            self.assertIn("可用指令", reply_text.await_args.args[1])
            reply_text.reset_mock()
            group = {
                "type": "message",
                "replyToken": "reply-token",
                "source": {"type": "group", "groupId": "G1", "userId": "U-win"},
                "message": {"id": "group-ai-1", "type": "text", "text": "明天我去哪個工地？"},
            }
            with patch.object(line_service, "reply_text", reply_text), patch.object(line_service, "reply_messages", reply_messages):
                await process_webhook_event(session, group)
            reply_text.assert_not_awaited()
            menu = {
                "type": "postback",
                "replyToken": "reply-token",
                "source": {"type": "user", "userId": "U-win"},
                "postback": {"data": "action=menu:switch-main"},
            }
            with patch.object(line_service, "reply_text", reply_text):
                await process_webhook_event(session, menu)
            reply_text.assert_not_awaited()

    async def test_disabled_confirm_button_does_not_save(self) -> None:
        parsed = intent(
            "assignment", work_date="2026-10-10", worksite_text="善捷47",
            employee_names=["勝忠"], work_item="物料搬運",
        )
        reply_text = AsyncMock(return_value=(True, "sent"))
        reply_messages = AsyncMock(return_value=(True, "sent"))
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            self._people(session)
            await self._send(session, "U-boss", "派勝忠", reply_text, reply_messages)
            data = reply_messages.await_args.args[1][0]["quickReply"]["items"][0]["action"]["data"]
            reply_text.reset_mock()
            with patch.object(settings, "ai_assistant_enabled", False):
                await self._send(session, "U-boss", "", reply_text, reply_messages, postback=data)
            self.assertIn("目前關閉", combined_reply(reply_text, reply_messages))
            self.assertEqual(len(session.exec(select(WorkAssignment)).all()), 0)

    async def test_parser_sends_structured_output_without_the_api_key(self) -> None:
        calls: list[dict] = []

        class FakeCompletions:
            async def parse(self, **kwargs):
                calls.append(kwargs)
                message = SimpleNamespace(parsed=intent("clock_in"), refusal=None)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.chat = SimpleNamespace(completions=FakeCompletions())

        with patch("openai.AsyncOpenAI", FakeClient):
            parsed = await parse_user_text("上班", today=date(2026, 10, 9))
        self.assertEqual(parsed.intent, "clock_in")
        self.assertEqual(calls[0]["model"], settings.openai_model)
        self.assertIs(calls[0]["response_format"], ParsedIntent)
        self.assertFalse(calls[0]["store"])
        blob = json.dumps(calls[0]["messages"], ensure_ascii=False)
        self.assertNotIn("sk-test-secret", blob)
        self.assertIn("2026-10-09", blob)

    def test_smoke_script_lists_the_owner_examples_and_does_not_write(self) -> None:
        script = Path(__file__).resolve().parents[1].joinpath("scripts", "ai_smoke_test.py").read_text(encoding="utf-8")
        for sentence in (
            "明天 47 標要兩台堆高機，勝忠跟建成去",
            "我下週一想排休",
            "10/20 請病假",
            "今天善捷工地做完了，堆高機搬了三車鋼筋",
            "上班",
            "我到工地了",
            "明天我去哪個工地？",
        ):
            self.assertIn(sentence, script)
        self.assertIn("不寫入資料庫", script)
        self.assertNotIn("sk-", script)

    def test_inactive_employee_exact_name_is_not_usable(self) -> None:
        from app.services.ai_assistant import match_employees

        with Session(self.engine) as session:
            session.add(Employee(
                employee_code="OLD001", name="舊同事", bind_token="ST-OLD",
                status=EmployeeStatus.inactive,
            ))
            session.commit()
            matched = match_employees(session, "舊同事")
            self.assertEqual(matched.status, "inactive")


if __name__ == "__main__":
    unittest.main()
