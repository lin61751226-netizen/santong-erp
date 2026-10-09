from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import employee_login_alias_statements, get_session
from app.core.security import hash_password, verify_password
from app.main import app
from app.models import AdminAuditLog, Employee, Role
from app.services.bootstrap import EMPLOYEE_ROSTER, _upsert_employee


class LoginAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(Employee(
                employee_code="BOSS001", name="林老闆", bind_token="ST-1001", role=Role.owner,
                password_hash=hash_password("Santong@2026"), must_change_password=True, status="active",
                line_user_id="U-boss",
            ))
            session.add(Employee(
                employee_code="ADMIN001", name="林金谷", bind_token="ST-1002", role=Role.admin,
                password_hash=hash_password("Santong@2026"), must_change_password=True, status="active",
                line_user_id="U-admin",
            ))
            session.add(Employee(
                employee_code="ADMIN002", name="秀蓉", bind_token="ST-1003", role=Role.admin,
                password_hash=hash_password("Santong@2026"), must_change_password=True, status="active",
                line_user_id="U-admin-2",
            ))
            session.add(Employee(
                employee_code="EMP001", name="勝忠", bind_token="ST-1005", role=Role.employee,
                password_hash=hash_password("Santong@2026"), must_change_password=True, status="active",
                line_user_id="U-worker",
            ))
            session.commit()

        def _override_get_session():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_session] = _override_get_session
        self.auth_backup = patch(
            "app.routes.auth.google_drive_worklog_service.backup_database",
            new=AsyncMock(return_value={"status": "saved"}),
        ).start()
        self.admin_backup = patch(
            "app.routes.admin.google_drive_worklog_service.backup_database",
            new=AsyncMock(return_value={"status": "saved"}),
        ).start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        patch.stopall()
        app.dependency_overrides.clear()

    def _login(self, code="ADMIN001", password="Santong@2026"):
        return self.client.post("/api/auth/login", json={"employee_code": code, "password": password})

    def _ready(self, code="ADMIN001", new_password="NewPass@2026"):
        login = self._login(code)
        self.assertEqual(login.status_code, 200, login.text)
        cookies = login.cookies
        changed = self.client.post(
            "/api/auth/change-password",
            json={"current_password": "Santong@2026", "new_password": new_password},
            cookies=cookies,
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        return cookies, new_password

    def _employee(self, code: str) -> Employee:
        with Session(self.engine) as session:
            return session.exec(select(Employee).where(Employee.employee_code == code)).one()

    def test_login_by_employee_code_still_works(self) -> None:
        response = self._login("ADMIN001")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["employee_code"], "ADMIN001")
        self.assertTrue(response.json()["must_change_password"])

    def test_login_by_alias_returns_the_same_employee_code(self) -> None:
        cookies, password = self._ready()
        before = self._employee("ADMIN001")
        session_key = before.session_key
        saved = self.client.post(
            "/api/auth/login-alias",
            json={"current_password": password, "login_alias": "  林老闆  "},
            cookies=cookies,
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["login_alias"], "林老闆")
        self.assertEqual(saved.json()["employee_code"], "ADMIN001")
        self.assertEqual(saved.json()["backup_status"], "saved")
        employee = self._employee("ADMIN001")
        self.assertEqual(employee.session_key, session_key)
        self.assertTrue(verify_password(password, employee.password_hash))
        self.assertEqual(employee.line_user_id, "U-admin")
        self.assertEqual(employee.employee_code, "ADMIN001")
        self.assertFalse(employee.must_change_password)
        me = self.client.get("/api/auth/me", cookies=cookies)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["login_alias"], "林老闆")
        self.assertEqual(me.json()["employee_code"], "ADMIN001")

        by_alias = self._login("林老闆", password)
        self.assertEqual(by_alias.status_code, 200, by_alias.text)
        self.assertEqual(by_alias.json()["employee_code"], "ADMIN001")
        padded = self._login("  林老闆  ", password)
        self.assertEqual(padded.status_code, 200, padded.text)
        with Session(self.engine) as session:
            audit = session.exec(select(AdminAuditLog).where(AdminAuditLog.action == "login_alias")).one()
            self.assertIn("林老闆", audit.summary)
            self.assertNotIn(password, audit.summary)

    def test_alias_is_unique_case_insensitively_and_cannot_match_a_code(self) -> None:
        cookies, password = self._ready("ADMIN001", "NewPass@2026")
        other_cookies, _other_password = self._ready("ADMIN002", "OtherPass@2026")
        first = self.client.post(
            "/api/auth/login-alias",
            json={"current_password": password, "login_alias": "Phone01"},
            cookies=cookies,
        )
        self.assertEqual(first.status_code, 200, first.text)
        folded = self._login("phone01", password)
        self.assertEqual(folded.status_code, 200, folded.text)
        self.assertEqual(folded.json()["employee_code"], "ADMIN001")
        duplicate = self.client.post(
            "/api/auth/login-alias",
            json={"current_password": "OtherPass@2026", "login_alias": " phone01 "},
            cookies=other_cookies,
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["detail"], "這個登入名稱已經有人使用")
        same_as_code = self.client.post(
            "/api/auth/login-alias",
            json={"current_password": "OtherPass@2026", "login_alias": "boss001"},
            cookies=other_cookies,
        )
        self.assertEqual(same_as_code.status_code, 400)
        self.assertEqual(same_as_code.json()["detail"], "登入名稱不能與員工代碼相同")
        too_long = self.client.post(
            "/api/auth/login-alias",
            json={"current_password": "OtherPass@2026", "login_alias": "名" * 33},
            cookies=other_cookies,
        )
        self.assertEqual(too_long.status_code, 400)
        self.assertIn("32", too_long.json()["detail"])
        self.assertIsNone(self._employee("ADMIN002").login_alias)

    def test_unknown_alias_uses_the_same_message_as_an_unknown_code(self) -> None:
        unknown_code = self._login("NOBODY")
        unknown_alias = self._login("沒有這個人")
        self.assertEqual(unknown_code.status_code, 401)
        self.assertEqual(unknown_alias.status_code, 401)
        self.assertEqual(unknown_code.json()["detail"], unknown_alias.json()["detail"])
        self.assertNotIn("登入名稱", unknown_alias.json()["detail"])

    def test_lockout_follows_the_account_for_code_and_alias(self) -> None:
        cookies, password = self._ready()
        saved = self.client.post(
            "/api/auth/login-alias",
            json={"current_password": password, "login_alias": "0912345678"},
            cookies=cookies,
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        unknown = self._login("沒有這個人")
        first = self._login("0912345678", "wrong-password")
        second = self._login("ADMIN001", "wrong-password")
        third = self._login("0912345678", "wrong-password")
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(first.status_code, 401)
        self.assertEqual(first.json()["detail"], unknown.json()["detail"])
        self.assertNotIn("還剩", first.json()["detail"])
        self.assertEqual(second.status_code, 401)
        self.assertIn("還剩", second.json()["detail"])
        self.assertEqual(third.status_code, 423)
        self.assertNotIn("0912345678", third.json()["detail"])
        self.assertEqual(self._login("ADMIN001", password).status_code, 423)
        self.assertEqual(self._login("0912345678", password).status_code, 423)

    def test_non_admin_role_still_forbidden_and_cannot_receive_an_alias(self) -> None:
        response = self._login("EMP001")
        self.assertEqual(response.status_code, 403)
        self.assertIn("無後台登入權限", response.json()["detail"])
        cookies, _password = self._ready("BOSS001", "OwnerPass@2026")
        rejected = self.client.put(
            "/api/employees/EMP001/login-alias",
            json={"login_alias": "勝忠"},
            cookies=cookies,
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIsNone(self._employee("EMP001").login_alias)
        self.assertEqual(self._login("勝忠").status_code, 401)

    def test_owner_can_set_an_admin_alias_without_resetting_secrets(self) -> None:
        self._ready("ADMIN002", "AdminTwo@2026")
        before = self._employee("ADMIN002")
        password_hash = before.password_hash
        session_key = before.session_key
        line_user_id = before.line_user_id
        cookies, _password = self._ready("BOSS001", "OwnerPass@2026")
        saved = self.client.put(
            "/api/employees/ADMIN002/login-alias",
            json={"login_alias": "秀蓉"},
            cookies=cookies,
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["employee_code"], "ADMIN002")
        self.assertEqual(saved.json()["backup_status"], "saved")
        after = self._employee("ADMIN002")
        self.assertEqual(after.login_alias, "秀蓉")
        self.assertEqual(after.password_hash, password_hash)
        self.assertEqual(after.session_key, session_key)
        self.assertEqual(after.line_user_id, line_user_id)
        self.assertEqual(after.employee_code, "ADMIN002")
        self.assertTrue(verify_password("AdminTwo@2026", after.password_hash))
        signed_in = self._login("秀蓉", "AdminTwo@2026")
        self.assertEqual(signed_in.status_code, 200)
        self.assertEqual(signed_in.json()["employee_code"], "ADMIN002")
        admin_cookies, _password = self._ready("ADMIN001", "NewPass@2026")
        forbidden = self.client.put(
            "/api/employees/ADMIN002/login-alias",
            json={"login_alias": "另一個名稱"},
            cookies=admin_cookies,
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(self._employee("ADMIN002").login_alias, "秀蓉")

    def test_clearing_alias_and_wrong_password_do_not_change_the_account(self) -> None:
        cookies, password = self._ready()
        self.client.post(
            "/api/auth/login-alias",
            json={"current_password": password, "login_alias": "林金谷"},
            cookies=cookies,
        )
        wrong = self.client.post(
            "/api/auth/login-alias",
            json={"current_password": "not-the-password", "login_alias": "別的名字"},
            cookies=cookies,
        )
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(self._employee("ADMIN001").login_alias, "林金谷")
        cleared = self.client.post(
            "/api/auth/login-alias",
            json={"current_password": password, "login_alias": "   "},
            cookies=cookies,
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertIsNone(cleared.json()["login_alias"])
        removed = self._login("林金谷", password)
        self.assertEqual(removed.status_code, 401)
        self.assertNotIn("登入名稱", removed.json()["detail"])
        self.assertEqual(self._login("ADMIN001", password).status_code, 200)
        forced = self.client.post(
            "/api/auth/login",
            json={"employee_code": "BOSS001", "password": "Santong@2026"},
        )
        denied = self.client.post(
            "/api/auth/login-alias",
            json={"current_password": "Santong@2026", "login_alias": "老闆"},
            cookies=forced.cookies,
        )
        self.assertEqual(denied.status_code, 403)
        self.assertIsNone(self._employee("BOSS001").login_alias)

    def test_login_form_offers_code_or_alias(self) -> None:
        html = Path(__file__).resolve().parents[1].joinpath("app/templates/index.html").read_text(encoding="utf-8")
        self.assertIn("員工代碼或登入名稱", html)
        self.assertIn("openLoginAlias()", html)
        self.assertIn("saveEmployeeLoginAlias", html)


class LoginAliasMigrationTests(unittest.TestCase):
    def test_alter_adds_the_column_and_keeps_existing_account_data(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE employee ("
                "id INTEGER PRIMARY KEY, employee_code VARCHAR, password_hash VARCHAR, "
                "line_user_id VARCHAR, session_key VARCHAR)"
            ))
            connection.execute(text(
                "INSERT INTO employee (employee_code, password_hash, line_user_id, session_key) "
                "VALUES ('ADMIN001', 'kept-hash', 'U-kept', 'kept-session')"
            ))
        columns = {column["name"] for column in inspect(engine).get_columns("employee")}
        self.assertNotIn("login_alias", columns)
        with engine.begin() as connection:
            for statement in employee_login_alias_statements(columns):
                connection.execute(text(statement))
        with engine.begin() as connection:
            row = connection.execute(text(
                "SELECT employee_code, password_hash, line_user_id, session_key, login_alias "
                "FROM employee WHERE employee_code = 'ADMIN001'"
            )).one()
        self.assertEqual(tuple(row), ("ADMIN001", "kept-hash", "U-kept", "kept-session", None))
        with self.assertRaises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text("UPDATE employee SET login_alias = 'LinBoss' WHERE employee_code = 'ADMIN001'"))
                connection.execute(text(
                    "INSERT INTO employee (employee_code, login_alias) VALUES ('ADMIN002', 'linboss')"
                ))

    def test_reseed_does_not_clear_alias_password_session_or_line_binding(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(Employee(
                employee_code="ADMIN001", name="舊名稱", bind_token="ST-old", role=Role.admin,
                password_hash="kept-hash", line_user_id="U-kept", session_key="kept-session",
                login_alias="林金谷", must_change_password=False, status="active",
            ))
            session.commit()
            payload = next(item for item in EMPLOYEE_ROSTER if item["employee_code"] == "ADMIN001")
            _upsert_employee(session, payload, {})
            session.commit()
            employee = session.exec(select(Employee).where(Employee.employee_code == "ADMIN001")).one()
            self.assertEqual(employee.login_alias, "林金谷")
            self.assertEqual(employee.password_hash, "kept-hash")
            self.assertEqual(employee.line_user_id, "U-kept")
            self.assertEqual(employee.session_key, "kept-session")
            self.assertEqual(employee.employee_code, "ADMIN001")
            self.assertFalse(employee.must_change_password)
