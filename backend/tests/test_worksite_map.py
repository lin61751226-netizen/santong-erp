from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import AdminAuditLog, AttendanceEvent, Employee, Role, Worksite
from app.services.nominatim import reset_rate_limit


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.headers = None
        self.params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, params=None, headers=None):
        self.params = params
        self.headers = headers
        _FakeClient.last = self
        return _FakeResponse([{
            "display_name": "桃園市龜山區文化一路",
            "lat": "25.047400",
            "lon": "121.387390",
        }])


class WorksiteMapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.backup = AsyncMock(return_value={"status": "saved"})
        self.backup_patcher = patch(
            "app.routes.admin.google_drive_worklog_service.backup_database",
            new=self.backup,
        )
        self.backup_patcher.start()
        reset_rate_limit()

    def tearDown(self) -> None:
        self.backup_patcher.stop()
        app.dependency_overrides.clear()
        self.engine.dispose()

    def _client(self, session: Session, actor: Employee) -> TestClient:
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_actor] = lambda: actor
        return TestClient(app)

    def test_map_point_saves_coordinates_without_clearing_google_link(self) -> None:
        with Session(self.engine) as session:
            admin = Employee(employee_code="ADMIN001", name="管理員", bind_token="ADMIN", role=Role.admin)
            worker = Employee(employee_code="EMP001", name="員工", bind_token="EMP", role=Role.employee)
            site = Worksite(
                code="金駿76",
                name="金駿76",
                address="文化一路",
                google_maps_url="https://www.google.com/maps/@24.8010,120.9910,17z",
                latitude=24.8010,
                longitude=120.9910,
                geofence_radius_m=120,
            )
            other = Worksite(code="56", name="56", latitude=25.05, longitude=121.40, geofence_radius_m=300)
            session.add_all([admin, worker, site, other])
            session.commit()
            session.add(AttendanceEvent(
                employee_id=admin.id, site_id=site.id, event_type="上班打卡",
                latitude=25.0474, longitude=121.38739, happened_at=datetime.utcnow(),
            ))
            session.add(AttendanceEvent(
                employee_id=worker.id, site_id=other.id, matched_site_id=site.id,
                event_type="下班打卡", latitude=25.0475, longitude=121.3874,
                happened_at=datetime.utcnow(),
            ))
            session.add(AttendanceEvent(
                employee_id=admin.id, site_id=site.id, event_type="上班打卡",
                happened_at=datetime.utcnow(),
            ))
            session.commit()
            client = self._client(session, admin)
            context = client.get(f"/api/worksites/{site.id}/map")
            self.assertEqual(context.status_code, 200)
            body = context.json()
            self.assertEqual(body["default_radius_m"], 400)
            self.assertEqual(body["google_maps_url"], site.google_maps_url)
            self.assertEqual([row["name"] for row in body["other_sites"]], ["56"])
            self.assertEqual(len(body["clock_points"]), 2)

            denied = self._client(session, worker).put(
                f"/api/worksites/{site.id}/location",
                json={"latitude": 25.0474, "longitude": 121.38739, "geofence_radius_m": 400},
            )
            self.assertEqual(denied.status_code, 403)
            app.dependency_overrides[get_current_actor] = lambda: admin
            invalid = client.put(
                f"/api/worksites/{site.id}/location",
                json={"latitude": 95, "longitude": 121.3, "geofence_radius_m": 400},
            )
            self.assertEqual(invalid.status_code, 422)
            partial = client.put(f"/api/worksites/{site.id}/location", json={"latitude": 25.0})
            self.assertEqual(partial.status_code, 400)
            self.assertEqual(partial.json()["detail"], "工地座標必須同時填寫緯度與經度")
            tiny = client.put(
                f"/api/worksites/{site.id}/location",
                json={"latitude": 25.0, "longitude": 121.0, "geofence_radius_m": 9},
            )
            self.assertEqual(tiny.status_code, 422)
            missing = client.get("/api/worksites/999/map")
            self.assertEqual(missing.status_code, 404)

            saved = client.put(f"/api/worksites/{site.id}/location", json={
                "latitude": 25.0474,
                "longitude": 121.38739,
                "geofence_radius_m": 450,
            })
            self.assertEqual(saved.status_code, 200)
            payload = saved.json()
            self.assertEqual(payload["backup_status"], "saved")
            self.assertAlmostEqual(payload["latitude"], 25.0474)
            self.assertEqual(payload["google_maps_url"], "https://www.google.com/maps/@24.8010,120.9910,17z")
            session.refresh(site)
            self.assertAlmostEqual(site.latitude, 25.0474)
            self.assertAlmostEqual(site.longitude, 121.38739)
            self.assertEqual(site.geofence_radius_m, 450)
            self.assertEqual(site.address, "文化一路")
            self.assertEqual(site.google_maps_url, "https://www.google.com/maps/@24.8010,120.9910,17z")
            audit = session.exec(select(AdminAuditLog).where(AdminAuditLog.action == "update")).one()
            self.assertIn("25.0474", audit.summary)
            self.assertIn("450", audit.summary)
            self.backup.assert_awaited()

            linked = client.put(f"/api/worksites/{site.id}/location", json={
                "google_maps_url": "https://www.google.com/maps/@24.9000,121.1000,17z",
                "latitude": 25.0474,
                "longitude": 121.38739,
            })
            self.assertEqual(linked.status_code, 200)
            session.refresh(site)
            self.assertAlmostEqual(site.latitude, 24.9000)
            self.assertAlmostEqual(site.longitude, 121.1000)
            self.assertEqual(site.geofence_radius_m, 450)
            client.close()

    async def test_geocode_uses_nominatim_and_rate_limit(self) -> None:
        with Session(self.engine) as session:
            admin = Employee(employee_code="ADMIN001", name="管理員", bind_token="ADMIN", role=Role.admin)
            worker = Employee(employee_code="EMP001", name="員工", bind_token="EMP", role=Role.employee)
            session.add_all([admin, worker])
            session.commit()
            client = self._client(session, admin)
            reset_rate_limit()
            with patch("app.services.nominatim.httpx.AsyncClient", _FakeClient):
                empty = client.get("/api/worksites/geocode", params={"q": "  "})
                self.assertEqual(empty.status_code, 400)
                found = client.get("/api/worksites/geocode", params={"q": "文化一路"})
                self.assertEqual(found.status_code, 200)
                self.assertEqual(found.json()["results"][0]["label"], "桃園市龜山區文化一路")
                self.assertAlmostEqual(found.json()["results"][0]["latitude"], 25.0474)
                self.assertIn("SantongERP", _FakeClient.last.headers["User-Agent"])
                self.assertEqual(_FakeClient.last.params["countrycodes"], "tw")
                busy = client.get("/api/worksites/geocode", params={"q": "文化一路"})
                self.assertEqual(busy.status_code, 429)
                self.assertIn("請稍候再搜尋", busy.json()["detail"])
            forbidden = self._client(session, worker).get("/api/worksites/geocode", params={"q": "文化一路"})
            self.assertEqual(forbidden.status_code, 403)
            client.close()
