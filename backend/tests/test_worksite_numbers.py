from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.config import settings
from app.models import (
    AssignmentMember,
    AttendanceEvent,
    Employee,
    Role,
    WorkAssignment,
    Worksite,
)
from app.services.ai_assistant import build_transcription_prompt, match_worksites
from app.services.line import line_service, process_webhook_event
from app.services.line_platform import line_platform_service
from app.services.speech_correction import correct_spoken_text, spoken_numbers
from tests.test_ai_assistant import intent


def _shown(*mocks: AsyncMock) -> str:
    parts: list[str] = []
    for mock in mocks:
        if not mock.await_count:
            continue
        payload = mock.await_args.args[1]
        if isinstance(payload, str):
            parts.append(payload)
            continue
        message = payload[0]
        parts.append(str(message.get("text") or ""))
        for item in message.get("quickReply", {}).get("items", []):
            action = item["action"]
            parts.append(str(action.get("label") or ""))
            parts.append(str(action.get("data") or action.get("text") or ""))
    return "\n".join(parts)


class WorksiteNumberTests(unittest.IsolatedAsyncioTestCase):
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
        self.voice = patch.object(settings, "ai_voice_enabled", True)
        self.voice.start()
        self.key = patch.object(settings, "openai_api_key", "sk-test-secret")
        self.key.start()
        self.today = patch("app.services.ai_assistant.local_today", return_value=date(2026, 10, 9))
        self.today.start()

    def tearDown(self) -> None:
        self.today.stop()
        self.key.stop()
        self.voice.stop()
        self.enabled.stop()
        self.backup.stop()
        self.engine.dispose()

    def _roster(self, session: Session) -> dict[str, Employee | Worksite]:
        sites = [
            Worksite(code="45", name="45", is_active=True),
            Worksite(code="53", name="齊裕53", is_active=True),
            Worksite(code="56", name="56", is_active=True),
            Worksite(code="善捷47", name="善捷47", is_active=True),
            Worksite(code="桃園29", name="桃園29", is_active=True),
            Worksite(code="新竹寶山1", name="新竹寶山1", is_active=True),
            Worksite(code="新竹寶山2", name="新竹寶山2", is_active=True),
        ]
        boss = Employee(
            employee_code="BOSS001", name="三通工程行林老闆", bind_token="ST-BOSS",
            role=Role.owner, line_user_id="U-boss",
        )
        sheng = Employee(
            employee_code="EMP001", name="勝忠", bind_token="ST-WIN",
            role=Role.employee, line_user_id="U-win",
        )
        session.add_all([*sites, boss, sheng])
        session.commit()
        by_name = {site.name: site for site in sites}
        by_name["boss"] = boss
        by_name["sheng"] = sheng
        return by_name

    def test_chinese_numerals_match_real_worksites_only(self) -> None:
        with Session(self.engine) as session:
            sites = self._roster(session)
            cases = {
                "五三": "齊裕53",
                "五十三": "齊裕53",
                "53": "齊裕53",
                "53標": "齊裕53",
                "午餐": "齊裕53",
                "午餐標": "齊裕53",
                "市七": "善捷47",
                "是七": "善捷47",
                "四七": "善捷47",
                "二九": "桃園29",
                "二十九": "桃園29",
                "五六": "56",
                "是五": "45",
                "也標": "新竹寶山1",
            }
            for query, expected in cases.items():
                matched = match_worksites(session, query)
                self.assertEqual(matched.status, "resolved", query)
                self.assertEqual(matched.records[0].name, expected, query)
            self.assertEqual(match_worksites(session, "吃午餐").status, "missing")
            self.assertEqual(match_worksites(session, "十五").status, "missing")
            self.assertEqual(match_worksites(session, "十標").status, "missing")
            self.assertEqual(spoken_numbers("十五"), ["15"])
            self.assertNotIn("45", spoken_numbers("十五"))
            self.assertIn("53", spoken_numbers("五三"))
            self.assertEqual(sites["齊裕53"].name, "齊裕53")

    def test_ten_is_not_guessed_when_both_4_and_10_exist(self) -> None:
        with Session(self.engine) as session:
            session.add_all([
                Worksite(code="4", name="4", is_active=True),
                Worksite(code="10", name="10", is_active=True),
            ])
            session.commit()
            matched = match_worksites(session, "十標")
            self.assertEqual(matched.status, "choices")
            self.assertEqual({site.name for site in matched.records}, {"4", "10"})

    def test_inactive_chinese_number_is_not_used(self) -> None:
        with Session(self.engine) as session:
            session.add(Worksite(code="53", name="齊裕53", is_active=False))
            session.commit()
            matched = match_worksites(session, "五三")
            self.assertEqual(matched.status, "inactive")

    def test_transcript_highlights_number_corrections(self) -> None:
        sites = [
            Worksite(code="53", name="齊裕53"),
            Worksite(code="善捷47", name="善捷47"),
            Worksite(code="桃園29", name="桃園29"),
            Worksite(code="56", name="56"),
        ]
        cases = {
            "明天午餐要兩台堆高機": ("明天53標要2台堆高機", "【53標】", "【2台】"),
            "明天市七標": ("明天47標", "【47標】", ""),
            "二九": ("29標", "【29標】", ""),
            "五六標": ("56標", "【56標】", ""),
            "十月二十號到五三": ("10月20日到53標", "【10月20日】", "【53標】"),
            "今天吃午餐": ("今天吃午餐", "", ""),
        }
        for raw, (plain, *highlights) in cases.items():
            fix = correct_spoken_text(raw, sites)
            self.assertEqual(fix.plain, plain, raw)
            for highlight in highlights:
                if highlight:
                    self.assertIn(highlight, fix.display, raw)
        untouched = correct_spoken_text("今天吃午餐", [Worksite(code="99", name="沒有53")])
        self.assertFalse(untouched.changed)
        no_site = correct_spoken_text("明天午餐要兩台", [Worksite(code="45", name="45")])
        self.assertNotIn("53", no_site.plain)
        self.assertIn("【2台】", no_site.display)

    def test_transcription_prompt_lists_real_labels_and_arabic_hint(self) -> None:
        with Session(self.engine) as session:
            self._roster(session)
            prompt = build_transcription_prompt(session)
        self.assertIn("47標", prompt)
        self.assertIn("53標", prompt)
        self.assertIn("29標", prompt)
        self.assertIn("56標", prompt)
        self.assertIn("阿拉伯數字", prompt)
        self.assertIn("午餐寫成53標", prompt)
        self.assertIn("市七寫成47標", prompt)
        self.assertIn("五三寫成53標", prompt)
        self.assertIn("二九寫成29標", prompt)
        self.assertIn("五六寫成56標", prompt)
        self.assertNotIn("sk-test-secret", prompt)

    async def _dispatch(self, session: Session, event: dict, reply_text, reply_messages, push):
        with patch.object(line_service, "reply_text", reply_text), patch.object(
            line_service, "reply_messages", reply_messages,
        ), patch.object(line_service, "push_messages", push):
            await process_webhook_event(session, event)

    def _mocks(self):
        return (
            AsyncMock(return_value=(True, "sent")),
            AsyncMock(return_value=(True, "sent")),
            AsyncMock(return_value=(True, "sent")),
        )

    async def test_typed_wusan_arrival_confirms_qiyu_53_before_save(self) -> None:
        parsed = intent("arrive_site", worksite_text="五三")
        reply_text, reply_messages, push = self._mocks()
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            self._roster(session)
            await self._dispatch(
                session,
                {
                    "type": "message",
                    "replyToken": "reply-token",
                    "source": {"type": "user", "userId": "U-win"},
                    "message": {"type": "text", "text": "到達工地,五三"},
                },
                reply_text, reply_messages, push,
            )
            text = _shown(reply_text, reply_messages, push)
            self.assertIn("請確認到達工地", text)
            self.assertIn("齊裕53", text)
            self.assertNotIn("找不到工地", text)
            self.assertNotIn("我聽到", text)
            self.assertEqual(len(session.exec(select(AttendanceEvent)).all()), 0)
            data = reply_messages.await_args.args[1][0]["quickReply"]["items"][0]["action"]["data"]
            self.assertTrue(data.startswith("action=ai:confirm:"))
            reply_text.reset_mock()
            reply_messages.reset_mock()
            await self._dispatch(
                session,
                {
                    "type": "postback",
                    "replyToken": "reply-token",
                    "source": {"type": "user", "userId": "U-win"},
                    "postback": {"data": data},
                },
                reply_text, reply_messages, push,
            )
            self.assertEqual(len(session.exec(select(AttendanceEvent)).all()), 1)
            self.assertIn("已記錄：到達工地", _shown(reply_text, reply_messages, push))

    async def test_unknown_worksite_offers_today_assignment_before_other_sites(self) -> None:
        parsed = intent("arrive_site", worksite_text="九九九")
        reply_text, reply_messages, push = self._mocks()
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            sites = self._roster(session)
            assignment = WorkAssignment(
                work_date=date(2026, 10, 9), site_id=sites["齊裕53"].id, work_item="堆高機",
            )
            session.add(assignment)
            session.commit()
            session.refresh(assignment)
            session.add(AssignmentMember(assignment_id=assignment.id, employee_id=sites["sheng"].id))
            session.commit()
            await self._dispatch(
                session,
                {
                    "type": "message",
                    "replyToken": "reply-token",
                    "source": {"type": "user", "userId": "U-win"},
                    "message": {"type": "text", "text": "到達工地,九九九"},
                },
                reply_text, reply_messages, push,
            )
            text = _shown(reply_text, reply_messages, push)
            self.assertIn("請點選正確的工地", text)
            self.assertNotIn("請用系統裡的工地名稱再說一次", text)
            buttons = reply_messages.await_args.args[1][0]["quickReply"]["items"]
            self.assertEqual(buttons[0]["action"]["label"], "齊裕53")
            self.assertIn("都不是", text)
            self.assertEqual(len(session.exec(select(AttendanceEvent)).all()), 0)
            reply_messages.reset_mock()
            await self._dispatch(
                session,
                {
                    "type": "postback",
                    "replyToken": "reply-token",
                    "source": {"type": "user", "userId": "U-win"},
                    "postback": {"data": buttons[0]["action"]["data"]},
                },
                reply_text, reply_messages, push,
            )
            confirmed = _shown(reply_text, reply_messages, push)
            self.assertIn("請確認到達工地", confirmed)
            self.assertIn("齊裕53", confirmed)
            self.assertEqual(len(session.exec(select(AttendanceEvent)).all()), 0)

    async def test_voice_homophones_show_raw_and_corrected_before_save(self) -> None:
        cases = [
            ("明天午餐要兩台堆高機，勝忠去", "53標", "午餐", "齊裕53"),
            ("明天市七要堆高機，勝忠去", "47標", "市七", "善捷47"),
            ("明天二九要堆高機，勝忠去", "29標", "二九", "桃園29"),
            ("明天五六要堆高機，勝忠去", "56標", "五六", "56"),
        ]
        with Session(self.engine) as session:
            self._roster(session)
            for raw, arabic, heard, site_name in cases:
                parsed = intent(
                    "assignment",
                    work_date="2026-10-10",
                    worksite_text=arabic,
                    employee_names=["勝忠"],
                    work_item="堆高機作業",
                )
                reply_text, reply_messages, push = self._mocks()
                parser = AsyncMock(return_value=parsed)
                with patch.object(
                    line_platform_service, "get_message_content", AsyncMock(return_value=(b"audio", "audio/mp4")),
                ), patch(
                    "app.services.ai_assistant.transcribe_audio", AsyncMock(return_value=raw),
                ), patch("app.services.ai_assistant.parse_user_text", new=parser):
                    await self._dispatch(
                        session,
                        {
                            "type": "message",
                            "replyToken": "reply-token",
                            "source": {"type": "user", "userId": "U-boss"},
                            "message": {
                                "type": "audio",
                                "id": "audio-1",
                                "duration": 3000,
                                "contentProvider": {"type": "line"},
                            },
                        },
                        reply_text, reply_messages, push,
                    )
                sent = parser.await_args.args[0]
                self.assertIn(arabic, sent, raw)
                self.assertNotIn(heard, sent, raw)
                self.assertIn("53標", parser.await_args.kwargs["context"])
                text = _shown(reply_text, reply_messages, push)
                self.assertIn(f"我聽到：{raw}", text)
                self.assertIn(f"【{arabic}】", text)
                self.assertIn("請確認派工", text)
                self.assertIn(site_name, text)
                self.assertEqual(len(session.exec(select(WorkAssignment)).all()), 0, raw)
