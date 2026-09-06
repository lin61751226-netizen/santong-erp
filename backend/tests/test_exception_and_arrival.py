from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.config import settings
from app.models import (
    AttendanceEvent, Employee, Forklift, Role, Worksite,
)
from app.services import forklift_service as fs
from app.services.hr import get_today_arrival_site
from app.services.line import line_service, process_webhook_event


def last_reply(mock):
    """reply_messages(reply_token, messages) → messages list"""
    return mock.await_args.args[1]


class ExceptionAndArrivalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.site = Worksite(code="S1", name="甲工地", is_active=True)
        self.driver = Employee(employee_code="E1", name="司機", bind_token="d", line_user_id="U-driver")
        self.boss = Employee(employee_code="B1", name="老闆", bind_token="b", role=Role.owner, line_user_id="U-boss")
        self.admin = Employee(employee_code="A1", name="管理員", bind_token="a", role=Role.admin, line_user_id="U-admin")
        self.session.add_all([self.site, self.driver, self.boss, self.admin])
        self.session.commit()
        self.vehicle = Forklift(forklift_code="1號", fuel_level=80, current_site_id=self.site.id)
        self.session.add(self.vehicle)
        self.session.commit()
        self.token = patch.object(settings, "line_channel_access_token", "test-token")
        self.token.start()
        self.push = AsyncMock(return_value=(True, "sent"))
        self.reply = AsyncMock(return_value=(True, "sent"))
        self.push_patch = patch.object(line_service, "push_text", self.push)
        self.reply_patch = patch.object(line_service, "reply_messages", self.reply)
        self.push_patch.start()
        self.reply_patch.start()

    def tearDown(self):
        fs.clear_session("U-driver")
        self.push_patch.stop()
        self.reply_patch.stop()
        self.token.stop()
        self.session.close()
        self.engine.dispose()

    async def send(self, text):
        await process_webhook_event(self.session, {
            "type": "message", "replyToken": "rt",
            "source": {"type": "user", "userId": "U-driver"},
            "message": {"type": "text", "text": text},
        })

    async def test_exception_menu_shows_quick_replies(self):
        await self.send("異常回報")
        messages = last_reply(self.reply)
        self.assertIn("quickReply", messages[0])
        texts = [item["action"]["text"] for item in messages[0]["quickReply"]["items"]]
        self.assertTrue(any("煞車異常" in x for x in texts))

    async def test_exception_detail_notifies_managers(self):
        await self.send("異常回報 煞車異常")
        pushed = {c.args[0] for c in self.push.await_args_list}
        self.assertEqual(pushed, {"U-admin", "U-boss"})
        messages = last_reply(self.reply)
        self.assertIn("煞車異常", messages[0]["text"])

    async def test_arrival_without_assignment_shows_site_choices(self):
        await self.send("到達工地")
        messages = last_reply(self.reply)
        self.assertIn("quickReply", messages[0])
        texts = [item["action"]["text"] for item in messages[0]["quickReply"]["items"]]
        self.assertIn(f"到達工地:{self.site.id}", texts)
        self.assertIsNone(get_today_arrival_site(self.session, self.driver.id))

    async def test_pick_arrival_site_records_event(self):
        await self.send(f"到達工地:{self.site.id}")
        arrived = get_today_arrival_site(self.session, self.driver.id)
        self.assertIsNotNone(arrived)
        self.assertEqual(arrived.id, self.site.id)
        events = self.session.exec(select(AttendanceEvent)).all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].site_id, self.site.id)


if __name__ == "__main__":
    unittest.main()
