from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.config import settings
from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import (
    AdminAuditLog, DeliveryStatus, Employee, Forklift, ForkliftInspection, ForkliftStatus,
    NotificationBatch, NotificationDelivery, Role, Worksite,
)
from app.services import forklift_service as fs
from app.services.bootstrap import _ensure_forklifts
from app.services.hr import record_attendance_event
from app.services.forklift_notifications import (
    deliver_forklift_notifications, queue_inspection_alert, queue_inspection_reminders, queue_vehicle_warning,
)
from app.services.line import line_service, process_webhook_event
from app.services.line_platform import build_default_rich_menu_payloads, generate_default_rich_menu_images


class ForkliftWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.site = Worksite(code="TEST", name="測試工地")
        self.driver = Employee(employee_code="TEST01", name="測試司機", bind_token="test", line_user_id="U-driver")
        self.boss = Employee(employee_code="BOSS", name="測試老闆", bind_token="boss", role=Role.owner, line_user_id="U-boss")
        self.admin = Employee(employee_code="ADMIN", name="測試管理員", bind_token="admin", role=Role.admin, line_user_id="U-admin")
        self.unbound = Employee(employee_code="UNBOUND", name="未綁定管理員", bind_token="unbound", role=Role.admin)
        self.inactive = Employee(employee_code="INACTIVE", name="停用管理員", bind_token="inactive", role=Role.admin, status="inactive", line_user_id="U-inactive")
        self.session.add_all([self.site, self.driver, self.boss, self.admin, self.unbound, self.inactive])
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
        app.dependency_overrides.clear()
        self.session.close()
        self.engine.dispose()

    async def send(self, text, group=False):
        await process_webhook_event(self.session, {
            "type": "message", "replyToken": "test-reply",
            "source": {"type": "group" if group else "user", "userId": "U-driver", "groupId": "test-group"},
            "message": {"type": "text", "text": text},
        })

    async def begin(self, group=False):
        await self.send("點檢", group)
        await self.send(f"點檢工地:{self.site.id}", group)
        await self.send(f"點檢堆高機:{self.vehicle.id}", group)

    async def test_complete_abnormal_flow_links_operator_and_notifies_all_managers(self):
        await self.begin()
        for item in fs.INSPECTION_ITEMS:
            await self.send("異常" if item["key"] == "brakes" else "正常")
        inspection = self.session.exec(select(ForkliftInspection)).one()
        self.assertFalse(inspection.all_passed)
        self.assertEqual(inspection.operator_id, self.driver.id)
        self.assertEqual(inspection.site_id, self.site.id)
        self.session.refresh(self.vehicle)
        self.assertEqual(self.vehicle.current_operator_id, self.driver.id)
        self.assertEqual(self.driver.line_user_id, "U-driver")
        self.assertIsNone(fs.get_session("U-driver"))
        self.assertEqual({c.args[0] for c in self.push.await_args_list}, {"U-admin", "U-boss"})
        self.assertTrue(all("剎車系統" in c.args[1] for c in self.push.await_args_list))
        delivery = self.session.exec(select(NotificationDelivery).where(NotificationDelivery.employee_id == self.unbound.id)).one()
        self.assertEqual(delivery.delivery_status, DeliveryStatus.skipped)

    async def test_group_chatter_does_not_advance_inspection(self):
        await self.begin(group=True)
        await self.send("大家辛苦了", group=True)
        self.assertEqual(fs.get_session("U-driver").current_item_index, 0)
        for _ in fs.INSPECTION_ITEMS:
            await self.send("正常", group=True)
        self.assertTrue(self.session.exec(select(ForkliftInspection)).one().all_passed)
        self.push.assert_not_awaited()
        self.reply.reset_mock()
        await self.send("正常", group=True)
        self.reply.assert_not_awaited()

    async def test_invalid_private_input_does_not_become_anomaly(self):
        await self.begin()
        await self.send("工作進度回報")
        self.assertEqual(fs.get_session("U-driver").current_item_index, 0)
        self.assertEqual(fs.get_session("U-driver").inspection_results, {})
        await self.send("取消點檢")
        self.assertIsNone(fs.get_session("U-driver"))

    async def test_failed_delivery_retries_without_resending_successes(self):
        async def fail_one(user_id, message):
            if user_id == "U-admin":
                raise TimeoutError("test")
            return True, "sent"
        self.push.side_effect = fail_one
        await self.begin()
        for _ in fs.INSPECTION_ITEMS:
            await self.send("異常")
        inspection = self.session.exec(select(ForkliftInspection)).one()
        delivery = self.session.exec(select(NotificationDelivery).where(NotificationDelivery.employee_id == self.admin.id)).one()
        self.assertEqual(delivery.delivery_status, DeliveryStatus.failed)
        self.push.reset_mock()
        self.push.side_effect = None
        queue_inspection_alert(self.session, inspection)
        await deliver_forklift_notifications(self.session)
        self.push.assert_awaited_once()
        self.assertEqual(self.push.await_args.args[0], "U-admin")

    async def test_reply_failure_does_not_lose_saved_inspection_or_alert(self):
        await self.begin()
        for _ in range(9):
            await self.send("異常")
        self.reply.side_effect = TimeoutError("reply expired")
        with self.assertRaises(TimeoutError):
            await self.send("正常")
        self.assertEqual(len(self.session.exec(select(ForkliftInspection)).all()), 1)
        self.assertEqual(self.push.await_count, 2)
        self.assertIsNone(fs.get_session("U-driver"))

    def test_incomplete_inspection_is_rejected(self):
        state = fs.InspectionSession("U-driver", self.driver.id, site_id=self.site.id, forklift_id=self.vehicle.id)
        with self.assertRaises(ValueError):
            fs.save_inspection(self.session, state)

    def test_warning_boundaries_and_overdue(self):
        today = date(2026, 9, 6)
        with patch.object(fs, "local_today", return_value=today):
            self.vehicle.fuel_level = 30
            self.vehicle.next_maintenance_date = today + timedelta(days=8)
            self.session.commit()
            self.assertEqual(fs.check_forklift_warnings(self.session, self.vehicle.id), [])
            self.vehicle.fuel_level = 29
            self.vehicle.next_maintenance_date = today + timedelta(days=7)
            self.session.commit()
            self.assertEqual(len(fs.check_forklift_warnings(self.session, self.vehicle.id)), 2)
            self.vehicle.fuel_level = None
            self.vehicle.next_maintenance_date = today - timedelta(days=1)
            self.session.commit()
            self.assertIn("已過期 1 天", fs.check_forklift_warnings(self.session, self.vehicle.id)[0])

    async def test_warning_daily_dedup_and_resolved_warning_not_retried(self):
        self.vehicle.fuel_level = 29
        self.vehicle.current_operator_id = self.driver.id
        self.session.commit()
        queue_vehicle_warning(self.session, self.vehicle)
        await deliver_forklift_notifications(self.session)
        self.assertEqual(self.push.await_count, 3)
        self.push.reset_mock()
        queue_vehicle_warning(self.session, self.vehicle)
        await deliver_forklift_notifications(self.session)
        self.push.assert_not_awaited()
        self.vehicle.fuel_level = 20
        self.session.commit()
        queue_vehicle_warning(self.session, self.vehicle)
        self.vehicle.fuel_level = 90
        self.session.commit()
        await deliver_forklift_notifications(self.session)
        self.push.assert_not_awaited()

    def test_seed_keeps_assigned_site_operator_and_inactive_status(self):
        self.vehicle.status = ForkliftStatus.inactive
        self.vehicle.current_operator_id = self.driver.id
        self.vehicle.next_maintenance_date = date(2026, 10, 1)
        self.session.commit()
        other_site = Worksite(code="OTHER", name="永森45")
        self.session.add(other_site)
        self.session.commit()
        _ensure_forklifts(self.session, {"永森45": other_site})
        self.session.refresh(self.vehicle)
        self.assertEqual(self.vehicle.current_site_id, self.site.id)
        self.assertEqual(self.vehicle.current_operator_id, self.driver.id)
        self.assertEqual(self.vehicle.status, ForkliftStatus.inactive)
        self.assertEqual(self.vehicle.next_maintenance_date, date(2026, 10, 1))

    def test_uninspected_operator_reminder_is_queued_once_and_delivered(self):
        self.driver.title = "堆高機司機"
        self.session.add(self.driver)
        self.session.commit()
        queue_inspection_reminders(self.session)
        queue_inspection_reminders(self.session)
        batches = self.session.exec(select(NotificationBatch).where(
            NotificationBatch.target_scope == "forklift_inspection_reminder",
        )).all()
        self.assertEqual(len(batches), 1)
        deliveries = self.session.exec(select(NotificationDelivery).where(
            NotificationDelivery.batch_id == batches[0].id,
        )).all()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].employee_id, self.driver.id)

    def api_client(self):
        app.dependency_overrides[get_session] = lambda: self.session
        app.dependency_overrides[get_current_actor] = lambda: self.admin
        return TestClient(app)

    def test_month_export_includes_full_period_and_unknown_items(self):
        for day in [date(2024, 1, 31), date(2024, 2, 1), date(2024, 2, 29), date(2024, 3, 1)]:
            self.session.add(ForkliftInspection(
                forklift_id=self.vehicle.id, operator_id=self.driver.id, site_id=self.site.id,
                inspection_date=day, inspection_items={"engine_oil": False}, all_passed=False, notes="=1+1",
            ))
        self.session.commit()
        client = self.api_client()
        params = {"month": "2024-02", "forklift_id": self.vehicle.id, "site_id": self.site.id}
        response = client.get("/api/forklift-inspections/export", params=params)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 3)
        self.assertEqual({r[0] for r in rows[1:]}, {"2024-02-01", "2024-02-29"})
        self.assertEqual(rows[1][6:8], ["異常", "未記錄"])
        self.assertEqual(rows[1][-1], "'=1+1")
        listing = client.get("/api/forklift-inspections", params=params).json()
        self.assertEqual(len(listing), 2)
        self.assertEqual(listing[0]["abnormal_items"], ["引擎機油"])
        all_listing = client.get("/api/forklift-inspections", params={"limit": 500}).json()
        self.assertEqual(len(all_listing), 4)
        self.assertEqual(
            {row["inspection_date"] for row in all_listing},
            {"2024-01-31", "2024-02-01", "2024-02-29", "2024-03-01"},
        )
        self.assertEqual(client.get("/api/forklift-inspections/export?month=2024-13").status_code, 422)
        self.assertEqual(client.get("/api/forklift-inspections/export?start_date=2026-09-30&end_date=2026-09-01").status_code, 422)

    def test_export_does_not_silently_truncate_at_500(self):
        self.session.add_all([ForkliftInspection(
            forklift_id=self.vehicle.id, operator_id=self.driver.id,
            inspection_date=date(2026, 9, 1), inspection_items={},
        ) for _ in range(501)])
        self.session.commit()
        client = self.api_client()
        response = client.get("/api/forklift-inspections/export?month=2026-09")
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 502)

    def test_care_update_validation_and_preserves_other_fields(self):
        client = self.api_client()
        path = f"/api/forklifts/{self.vehicle.id}/care"
        self.assertEqual(client.put(path, json={"fuel_level": 101}).status_code, 422)
        self.assertEqual(client.put(path, json={"fuel_level": -1}).status_code, 422)
        self.assertEqual(client.put(path, json={"fuel_level": 30, "next_maintenance_date": "2099-01-01"}).status_code, 200)
        self.session.refresh(self.vehicle)
        self.assertEqual(self.vehicle.fuel_level, 30)
        self.assertEqual(self.vehicle.current_site_id, self.site.id)
        self.assertEqual(self.vehicle.next_maintenance_date, date(2099, 1, 1))
        self.assertEqual(client.put(path, json={"next_maintenance_date": None}).status_code, 200)
        self.assertEqual(self.vehicle.fuel_level, 30)
        self.assertIsNone(self.vehicle.next_maintenance_date)
        app.dependency_overrides.pop(get_current_actor)
        self.assertEqual(client.get("/api/forklift-inspections/export?month=2026-09").status_code, 401)
        self.assertEqual(client.put(path, json={"fuel_level": 20}).status_code, 401)
        self.assertEqual(client.get("/api/forklift-notifications").status_code, 401)

    def test_worksite_location_and_lifecycle_are_audited(self):
        client = self.api_client()
        created = client.post("/api/worksites", json={
            "code": "GPS-1", "name": "GPS 測試工地", "address": "測試路 1 號",
            "latitude": 24.8000, "longitude": 120.9900, "geofence_radius_m": 120,
        })
        self.assertEqual(created.status_code, 201)
        site_id = created.json()["worksite"]["id"]
        location = client.put(f"/api/worksites/{site_id}/location", json={
            "latitude": 24.8010, "longitude": 120.9910, "geofence_radius_m": 180,
        })
        self.assertEqual(location.status_code, 200)
        self.assertEqual(self.session.get(Worksite, site_id).geofence_radius_m, 180)
        self.assertEqual(client.delete(f"/api/worksites/{site_id}").status_code, 200)
        self.assertFalse(self.session.get(Worksite, site_id).is_active)
        self.assertEqual(client.post(f"/api/worksites/{site_id}/restore").status_code, 200)
        self.assertTrue(self.session.get(Worksite, site_id).is_active)
        audit_rows = self.session.exec(select(AdminAuditLog).where(
            AdminAuditLog.entity_id == site_id,
        )).all()
        self.assertEqual({row.action for row in audit_rows}, {"create", "update", "deactivate", "restore"})

    def test_attendance_outside_configured_site_radius_is_marked(self):
        self.site.latitude = 24.8000
        self.site.longitude = 120.9900
        self.site.geofence_radius_m = 100
        self.driver.home_site_id = self.site.id
        self.session.add_all([self.site, self.driver])
        self.session.commit()
        result = record_attendance_event(
            self.session, self.driver, "上班打卡", latitude=24.8100, longitude=120.9900,
        )
        self.assertTrue(any("定位超出工地範圍" in item for item in result.anomalies))
        self.assertIn("定位超出工地範圍", result.event.note)

    def test_rich_menu_button_and_bundled_image_agree(self):
        payload = build_default_rich_menu_payloads("https://example.test")["tools"]
        items = [area for area in payload["areas"] if area["action"].get("text") == "點檢"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["bounds"], {"x": 500, "y": 250, "width": 500, "height": 1436})
        with TemporaryDirectory() as temp, patch("app.services.line_platform._load_font", side_effect=AssertionError("Must use bundled CJK image")):
            outputs = generate_default_rich_menu_images(output_dir=Path(temp))
            self.assertEqual(outputs["tools"].name, "santong-tools-inspection-v1.png")
            self.assertGreater(outputs["tools"].stat().st_size, 1000)
