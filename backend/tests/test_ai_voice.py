from __future__ import annotations

import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.config import Settings, settings
from app.models import (
    AiInteractionLog,
    AssignmentMember,
    Employee,
    Role,
    WorkAssignment,
    Worksite,
)
from app.services.ai_assistant import (
    MAX_AUDIO_BYTES,
    VOICE_DISABLED_TEXT,
    VOICE_FAILURE_TEXT,
    VOICE_TOO_LARGE_TEXT,
    VOICE_TOO_LONG_TEXT,
    ParsedIntent,
)
from app.services.forklift_service import clear_session, start_session
from app.services.line import _pending_location_attendance, line_service, process_webhook_event
from app.services.line_platform import line_platform_service


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


def _chunks(mock: AsyncMock) -> list[str]:
    if not mock.await_count:
        return []
    payload = mock.await_args.args[1]
    if isinstance(payload, str):
        return [payload]
    message = payload[0]
    parts = [str(message.get("text") or "")]
    for item in message.get("quickReply", {}).get("items", []):
        action = item["action"]
        parts.append(str(action.get("label") or ""))
        parts.append(str(action.get("data") or action.get("text") or ""))
    return parts


def shown(*mocks: AsyncMock) -> str:
    parts: list[str] = []
    for mock in mocks:
        parts.extend(_chunks(mock))
    return "\n".join(parts)


class _CaptureClient:
    last: "_CaptureClient | None" = None

    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs
        self.create = AsyncMock(return_value=SimpleNamespace(text="明天 47 標要兩台堆高機，勝忠跟建成去"))
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self.create))
        _CaptureClient.last = self


class AiVoiceTests(unittest.IsolatedAsyncioTestCase):
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
        self.model = patch.object(settings, "openai_transcribe_model", "gpt-4o-mini-transcribe")
        self.model.start()
        self.today = patch("app.services.ai_assistant.local_today", return_value=date(2026, 10, 9))
        self.today.start()

    def tearDown(self) -> None:
        _pending_location_attendance.clear()
        clear_session("U-boss")
        clear_session("U-win")
        self.today.stop()
        self.model.stop()
        self.key.stop()
        self.voice.stop()
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

    def _audio_event(
        self,
        user_id: str,
        *,
        duration: int = 4000,
        source_type: str = "user",
        provider: str | None = "line",
        message_id: str = "audio-1",
    ) -> dict:
        message: dict = {"type": "audio", "id": message_id, "duration": duration}
        if provider is not None:
            message["contentProvider"] = {"type": provider}
        return {
            "type": "message",
            "replyToken": "reply-token",
            "source": {"type": source_type, "userId": user_id, "groupId": "G-1"},
            "message": message,
        }

    async def _dispatch(self, session: Session, event: dict, reply_text, reply_messages, push):
        with patch.object(line_service, "reply_text", reply_text), patch.object(
            line_service, "reply_messages", reply_messages,
        ), patch.object(line_service, "push_messages", push):
            await process_webhook_event(session, event)

    def _mocks(self, reply_ok: tuple[bool, str] = (True, "sent")):
        return (
            AsyncMock(return_value=(True, "sent")),
            AsyncMock(return_value=reply_ok),
            AsyncMock(return_value=(True, "sent")),
        )

    async def test_private_audio_confirms_transcript_then_saves_on_confirm(self) -> None:
        transcript = "明天 47 標要兩台堆高機，勝忠跟建成去"
        parsed = intent(
            "assignment",
            work_date="2026-10-10",
            worksite_text="47標",
            employee_names=["勝忠", "建成"],
            equipment_text="堆高機",
            equipment_count=2,
        )
        reply_text, reply_messages, push = self._mocks()
        content = AsyncMock(return_value=(b"ID3-not-stored", "audio/mp4"))
        transcribe = AsyncMock(return_value=transcript)
        with Session(self.engine) as session, patch.object(
            line_platform_service, "get_message_content", content,
        ), patch("app.services.ai_assistant.transcribe_audio", transcribe), patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            people = self._people(session)
            await self._dispatch(session, self._audio_event("U-boss"), reply_text, reply_messages, push)
            text = shown(reply_text, reply_messages, push)
            self.assertIn("我聽到：", text)
            self.assertIn(transcript, text)
            self.assertIn("請確認派工", text)
            self.assertEqual(push.await_count, 0)
            self.assertEqual(len(session.exec(select(WorkAssignment)).all()), 0)
            data = reply_messages.await_args.args[1][0]["quickReply"]["items"][0]["action"]["data"]
            self.assertTrue(data.startswith("action=ai:confirm:"))
            logged = session.exec(select(AiInteractionLog)).all()
            self.assertTrue(any(row.input_text == transcript for row in logged))
            blob = json.dumps([row.model_dump() for row in logged], ensure_ascii=False, default=str)
            self.assertNotIn("sk-test-secret", blob)
            self.assertNotIn("ID3-not-stored", blob)
            self.assertIn("source=voice", blob)
            reply_text.reset_mock()
            reply_messages.reset_mock()
            push.reset_mock()
            await self._dispatch(
                session,
                {
                    "type": "postback",
                    "replyToken": "reply-token",
                    "source": {"type": "user", "userId": "U-boss"},
                    "postback": {"data": data},
                },
                reply_text,
                reply_messages,
                push,
            )
            saved = session.exec(select(WorkAssignment)).one()
            self.assertEqual(saved.site_id, people["site"].id)
            member_ids = {
                row.employee_id
                for row in session.exec(select(AssignmentMember).where(AssignmentMember.assignment_id == saved.id)).all()
            }
            self.assertEqual(member_ids, {people["sheng"].id, people["ray"].id})
            self.assertNotIn("我聽到", shown(reply_text, reply_messages, push))

    async def test_group_audio_is_not_transcribed(self) -> None:
        reply_text, reply_messages, push = self._mocks()
        content = AsyncMock(return_value=(b"audio", "audio/mp4"))
        transcribe = AsyncMock(return_value="明天派工")
        with Session(self.engine) as session, patch.object(
            line_platform_service, "get_message_content", content,
        ), patch("app.services.ai_assistant.transcribe_audio", transcribe):
            self._people(session)
            await self._dispatch(
                session,
                self._audio_event("U-boss", source_type="group"),
                reply_text,
                reply_messages,
                push,
            )
            self.assertEqual(content.await_count, 0)
            self.assertEqual(transcribe.await_count, 0)
            self.assertEqual(reply_text.await_count, 0)
            self.assertEqual(reply_messages.await_count, 0)
            self.assertEqual(push.await_count, 0)
            self.assertEqual(len(session.exec(select(AiInteractionLog)).all()), 0)

    async def test_long_audio_is_rejected_before_download(self) -> None:
        reply_text, reply_messages, push = self._mocks()
        content = AsyncMock()
        with Session(self.engine) as session, patch.object(line_platform_service, "get_message_content", content):
            self._people(session)
            await self._dispatch(
                session, self._audio_event("U-boss", duration=120_000), reply_text, reply_messages, push,
            )
            self.assertEqual(content.await_count, 0)
            self.assertIn(VOICE_TOO_LONG_TEXT, shown(reply_text, reply_messages, push))

    async def test_oversized_audio_is_not_transcribed(self) -> None:
        reply_text, reply_messages, push = self._mocks()
        content = AsyncMock(return_value=(b"x" * (MAX_AUDIO_BYTES + 1), "audio/mp4"))
        transcribe = AsyncMock(return_value="不該被呼叫")
        with Session(self.engine) as session, patch.object(
            line_platform_service, "get_message_content", content,
        ), patch("app.services.ai_assistant.transcribe_audio", transcribe):
            self._people(session)
            await self._dispatch(session, self._audio_event("U-boss"), reply_text, reply_messages, push)
            self.assertEqual(transcribe.await_count, 0)
            self.assertIn(VOICE_TOO_LARGE_TEXT, shown(reply_text, reply_messages, push))

    async def test_transcription_failure_replies_without_raising(self) -> None:
        reply_text, reply_messages, push = self._mocks()
        content = AsyncMock(return_value=(b"audio", "audio/mp4"))
        transcribe = AsyncMock(side_effect=RuntimeError("boom sk-test-secret"))
        with Session(self.engine) as session, patch.object(
            line_platform_service, "get_message_content", content,
        ), patch("app.services.ai_assistant.transcribe_audio", transcribe):
            self._people(session)
            await self._dispatch(session, self._audio_event("U-boss"), reply_text, reply_messages, push)
            self.assertIn(VOICE_FAILURE_TEXT, shown(reply_text, reply_messages, push))
            log = session.exec(select(AiInteractionLog)).one()
            self.assertEqual(log.outcome, "fallback")
            self.assertEqual(log.detail.split(";")[0], "RuntimeError")
            self.assertNotIn("boom", log.detail or "")
            self.assertNotIn("sk-test-secret", log.detail or "")
            self.assertEqual(log.input_text, "[語音]")

    async def test_assistant_off_does_not_transcribe(self) -> None:
        reply_text, reply_messages, push = self._mocks()
        transcribe = AsyncMock(return_value="明天派工")
        with Session(self.engine) as session, patch.object(settings, "ai_assistant_enabled", False), patch(
            "app.services.ai_assistant.transcribe_audio", transcribe,
        ):
            self._people(session)
            await self._dispatch(session, self._audio_event("U-boss"), reply_text, reply_messages, push)
            self.assertEqual(transcribe.await_count, 0)
            self.assertIn(VOICE_DISABLED_TEXT, shown(reply_text, reply_messages, push))
            self.assertEqual(len(session.exec(select(AiInteractionLog)).all()), 0)

    async def test_voice_flag_off_does_not_transcribe(self) -> None:
        reply_text, reply_messages, push = self._mocks()
        transcribe = AsyncMock(return_value="明天派工")
        with Session(self.engine) as session, patch.object(settings, "ai_voice_enabled", False), patch(
            "app.services.ai_assistant.transcribe_audio", transcribe,
        ):
            self._people(session)
            await self._dispatch(session, self._audio_event("U-boss"), reply_text, reply_messages, push)
            self.assertEqual(transcribe.await_count, 0)
            self.assertIn(VOICE_DISABLED_TEXT, shown(reply_text, reply_messages, push))

    async def test_failed_reply_falls_back_to_push(self) -> None:
        transcript = "明天派勝忠去善捷47"
        parsed = intent(
            "assignment", work_date="2026-10-10", worksite_text="善捷47",
            employee_names=["勝忠"], work_item="物料搬運",
        )
        reply_text, reply_messages, push = self._mocks(reply_ok=(False, "expired"))
        with Session(self.engine) as session, patch.object(
            line_platform_service, "get_message_content", AsyncMock(return_value=(b"audio", "audio/mp4")),
        ), patch("app.services.ai_assistant.transcribe_audio", AsyncMock(return_value=transcript)), patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            self._people(session)
            await self._dispatch(session, self._audio_event("U-boss"), reply_text, reply_messages, push)
            self.assertEqual(reply_messages.await_count, 1)
            self.assertEqual(push.await_count, 1)
            pushed = push.await_args.args[1][0]
            self.assertIn("我聽到：", pushed["text"])
            self.assertIn(transcript, pushed["text"])
            self.assertIn("quickReply", pushed)
            self.assertEqual(push.await_args.args[0], "U-boss")

    async def test_slow_turn_pushes_without_using_reply_token(self) -> None:
        transcript = "明天派勝忠去善捷47"
        parsed = intent(
            "assignment", work_date="2026-10-10", worksite_text="善捷47",
            employee_names=["勝忠"], work_item="物料搬運",
        )
        reply_text, reply_messages, push = self._mocks()
        with Session(self.engine) as session, patch(
            "app.services.ai_assistant.VOICE_REPLY_BUDGET_SECONDS", 0,
        ), patch.object(
            line_platform_service, "get_message_content", AsyncMock(return_value=(b"audio", "audio/mp4")),
        ), patch("app.services.ai_assistant.transcribe_audio", AsyncMock(return_value=transcript)), patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            self._people(session)
            await self._dispatch(session, self._audio_event("U-boss"), reply_text, reply_messages, push)
            self.assertEqual(reply_messages.await_count, 0)
            self.assertEqual(reply_text.await_count, 0)
            self.assertEqual(push.await_count, 1)
            self.assertIn("我聽到：", push.await_args.args[1][0]["text"])

    async def test_text_clock_in_does_not_download_audio(self) -> None:
        reply_text, reply_messages, push = self._mocks()
        content = AsyncMock()
        transcribe = AsyncMock()
        with Session(self.engine) as session, patch.object(
            line_platform_service, "get_message_content", content,
        ), patch("app.services.ai_assistant.transcribe_audio", transcribe):
            self._people(session)
            await self._dispatch(
                session,
                {
                    "type": "message",
                    "replyToken": "reply-token",
                    "source": {"type": "user", "userId": "U-win"},
                    "message": {"type": "text", "text": "上班打卡"},
                },
                reply_text,
                reply_messages,
                push,
            )
            self.assertEqual(content.await_count, 0)
            self.assertEqual(transcribe.await_count, 0)
            self.assertEqual(push.await_count, 0)

    async def test_unbound_audio_asks_to_bind(self) -> None:
        reply_text, reply_messages, push = self._mocks()
        transcribe = AsyncMock()
        with Session(self.engine) as session, patch("app.services.ai_assistant.transcribe_audio", transcribe):
            self._people(session)
            await self._dispatch(session, self._audio_event("U-stranger"), reply_text, reply_messages, push)
            self.assertEqual(transcribe.await_count, 0)
            self.assertIn("開始綁定", shown(reply_text, reply_messages, push))

    async def test_transcription_call_uses_language_hint_and_roster(self) -> None:
        parsed = intent("unknown")
        reply_text, reply_messages, push = self._mocks()
        _CaptureClient.last = None
        with Session(self.engine) as session, patch.object(
            line_platform_service, "get_message_content", AsyncMock(return_value=(b"raw-audio", "audio/mp4")),
        ), patch("openai.AsyncOpenAI", _CaptureClient), patch(
            "app.services.ai_assistant.parse_user_text", new=AsyncMock(return_value=parsed),
        ):
            self._people(session)
            await self._dispatch(session, self._audio_event("U-boss"), reply_text, reply_messages, push)
            client = _CaptureClient.last
            self.assertIsNotNone(client)
            self.assertEqual(client.init_kwargs["timeout"], 20)
            self.assertEqual(client.init_kwargs["max_retries"], 0)
            kwargs = client.create.await_args.kwargs
            self.assertEqual(kwargs["language"], "zh")
            self.assertEqual(kwargs["model"], "gpt-4o-mini-transcribe")
            self.assertIn("勝忠", kwargs["prompt"])
            self.assertIn("善捷47", kwargs["prompt"])
            self.assertNotIn("sk-test-secret", kwargs["prompt"])
            filename, content, content_type = kwargs["file"]
            self.assertEqual(filename, "voice.m4a")
            self.assertEqual(content, b"raw-audio")
            self.assertEqual(content_type, "audio/mp4")
            self.assertIn("我聽到：", shown(reply_text, reply_messages, push))
            blob = json.dumps(
                [row.model_dump() for row in session.exec(select(AiInteractionLog)).all()],
                ensure_ascii=False,
                default=str,
            )
            self.assertNotIn("raw-audio", blob)
            self.assertNotIn("sk-test-secret", blob)

    async def test_help_mentions_voice_only_when_both_flags_are_on(self) -> None:
        reply_text, reply_messages, push = self._mocks()
        event = {
            "type": "message",
            "replyToken": "reply-token",
            "source": {"type": "user", "userId": "U-win"},
            "message": {"type": "text", "text": "指令"},
        }
        with Session(self.engine) as session:
            self._people(session)
            await self._dispatch(session, event, reply_text, reply_messages, push)
            self.assertIn("直接傳語音", shown(reply_text, reply_messages, push))
            reply_text.reset_mock()
            with patch.object(settings, "ai_voice_enabled", False):
                await self._dispatch(session, event, reply_text, reply_messages, push)
            self.assertNotIn("直接傳語音", shown(reply_text, reply_messages, push))

    async def test_inspection_audio_is_not_transcribed(self) -> None:
        reply_text, reply_messages, push = self._mocks()
        content = AsyncMock()
        with Session(self.engine) as session, patch.object(line_platform_service, "get_message_content", content):
            people = self._people(session)
            start_session("U-win", people["sheng"].id)
            await self._dispatch(session, self._audio_event("U-win"), reply_text, reply_messages, push)
            self.assertEqual(content.await_count, 0)
            self.assertIn("點檢進行中", shown(reply_text, reply_messages, push))

    def test_voice_settings_default_on_and_model_blank_uses_mini_transcribe(self) -> None:
        enabled = Settings(ai_voice_enabled="", openai_transcribe_model="  ")
        self.assertTrue(enabled.ai_voice_enabled)
        self.assertEqual(enabled.openai_transcribe_model, "gpt-4o-mini-transcribe")
        self.assertFalse(Settings(ai_voice_enabled="off").ai_voice_enabled)
        self.assertTrue(Settings(ai_voice_enabled="yes").ai_voice_enabled)
