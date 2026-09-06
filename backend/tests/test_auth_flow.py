from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.core.security import hash_password
from app.main import app
from app.models import Employee, Role


class AuthFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(
                Employee(
                    employee_code="BOSS001",
                    name="林老闆",
                    bind_token="ST-1001",
                    role=Role.owner,
                    password_hash=hash_password("Santong@2026"),
                    must_change_password=True,
                    status="active",
                )
            )
            session.add(
                Employee(
                    employee_code="ADMIN001",
                    name="林金谷",
                    bind_token="ST-1002",
                    role=Role.admin,
                    password_hash=hash_password("Santong@2026"),
                    must_change_password=True,
                    status="active",
                )
            )
            session.add(
                Employee(
                    employee_code="EMP001",
                    name="勝忠",
                    bind_token="ST-1005",
                    role=Role.employee,
                    password_hash=hash_password("Santong@2026"),
                    must_change_password=True,
                    status="active",
                )
            )
            session.commit()

        def _override_get_session():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_session] = _override_get_session
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    def _login(self, code="ADMIN001", password="Santong@2026"):
        return self.client.post(
            "/api/auth/login",
            json={"employee_code": code, "password": password},
        )

    def test_login_success_with_default_password(self) -> None:
        response = self._login()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["employee_code"], "ADMIN001")
        self.assertTrue(data["must_change_password"])
        self.assertIn("santong_session", response.cookies)

    def test_unauthenticated_dashboard_returns_401(self) -> None:
        response = self.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 401)

    def test_rich_menu_maintenance_endpoints_require_login(self) -> None:
        for path in (
            "/api/line-management/richmenu/debug",
            "/api/line-management/richmenu/relink-users",
            "/api/line-management/richmenu/cleanup",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_must_change_password_blocks_admin_api(self) -> None:
        login_response = self._login()
        cookies = login_response.cookies
        blocked = self.client.get("/api/dashboard", cookies=cookies)
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("變更密碼", blocked.json()["detail"])

    def test_change_password_then_access_dashboard(self) -> None:
        login_response = self._login()
        cookies = login_response.cookies
        change = self.client.post(
            "/api/auth/change-password",
            json={
                "current_password": "Santong@2026",
                "new_password": "NewPass@2026",
            },
            cookies=cookies,
        )
        self.assertEqual(change.status_code, 200)

        dashboard = self.client.get("/api/dashboard", cookies=cookies)
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("employee_count", dashboard.json())

    def test_login_lockout_after_3_failures(self) -> None:
        for attempt in range(1, 4):
            response = self._login(password="wrong-password")
            if attempt < 3:
                self.assertEqual(response.status_code, 401)
            else:
                self.assertEqual(response.status_code, 423)
                self.assertIn("鎖定", response.json()["detail"])

        # 鎖定期間即使密碼正確也無法登入
        locked = self._login()
        self.assertEqual(locked.status_code, 423)

    def test_non_owner_admin_cannot_login(self) -> None:
        response = self._login(code="EMP001")
        self.assertEqual(response.status_code, 403)

    def test_old_password_invalid_after_change(self) -> None:
        login_response = self._login()
        cookies = login_response.cookies
        self.client.post(
            "/api/auth/change-password",
            json={
                "current_password": "Santong@2026",
                "new_password": "BrandNew@99",
            },
            cookies=cookies,
        )
        old_password_login = self._login(password="Santong@2026")
        self.assertEqual(old_password_login.status_code, 401)

        new_password_login = self._login(password="BrandNew@99")
        self.assertEqual(new_password_login.status_code, 200)
        self.assertFalse(new_password_login.json()["must_change_password"])

    def test_logout_invalidates_session(self) -> None:
        login_response = self._login()
        cookies = login_response.cookies
        logout = self.client.post("/api/auth/logout", cookies=cookies)
        self.assertEqual(logout.status_code, 200)
        after_logout = self.client.get("/api/dashboard", cookies=cookies)
        self.assertEqual(after_logout.status_code, 401)

    def test_expired_session_is_rejected(self) -> None:
        login_response = self._login()
        cookies = login_response.cookies
        with Session(self.engine) as session:
            admin = session.exec(
                select(Employee).where(Employee.employee_code == "ADMIN001")
            ).first()
            admin.session_expires_at = datetime.utcnow() - timedelta(minutes=1)
            session.add(admin)
            session.commit()
        expired = self.client.get("/api/dashboard", cookies=cookies)
        self.assertEqual(expired.status_code, 401)

    def test_reset_password_restores_default(self) -> None:
        login_response = self._login()
        cookies = login_response.cookies
        self.client.post(
            "/api/auth/change-password",
            json={
                "current_password": "Santong@2026",
                "new_password": "Custom@123",
            },
            cookies=cookies,
        )
        # 管理者重設他人密碼
        reset = self.client.post(
            "/api/auth/reset-password",
            json={"employee_code": "ADMIN001", "new_password": "ignored"},
        )
        self.assertEqual(reset.status_code, 200)
        # 重設後以預設密碼登入，需再次改密碼
        again = self._login(password="Santong@2026")
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()["must_change_password"])
