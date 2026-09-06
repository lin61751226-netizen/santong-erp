from contextlib import contextmanager

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.core.config import settings


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, echo=False, connect_args=connect_args)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _apply_lightweight_migrations()


def get_session():
    with Session(engine) as session:
        yield session


@contextmanager
def session_scope():
    with Session(engine) as session:
        yield session


def _apply_lightweight_migrations() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    if "leaverequest" in table_names:
        columns = {column["name"] for column in inspector.get_columns("leaverequest")}
        if "policy_note" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE leaverequest ADD COLUMN policy_note VARCHAR"))

    if "assignmentmember" in table_names:
        columns = {column["name"] for column in inspector.get_columns("assignmentmember")}
        statements = []
        if "is_active" not in columns:
            statements.append("ALTER TABLE assignmentmember ADD COLUMN is_active BOOLEAN DEFAULT 1")
        if "replacement_for_employee_id" not in columns:
            statements.append("ALTER TABLE assignmentmember ADD COLUMN replacement_for_employee_id INTEGER")
        if statements:
            with engine.begin() as connection:
                for statement in statements:
                    connection.execute(text(statement))

    if "employee" in table_names:
        columns = {column["name"] for column in inspector.get_columns("employee")}
        statements = []
        if "email" not in columns:
            statements.append("ALTER TABLE employee ADD COLUMN email VARCHAR")
        if "assigned_sites" not in columns:
            statements.append("ALTER TABLE employee ADD COLUMN assigned_sites JSON")
        if "password_hash" not in columns:
            statements.append("ALTER TABLE employee ADD COLUMN password_hash VARCHAR")
        if "failed_login_count" not in columns:
            statements.append("ALTER TABLE employee ADD COLUMN failed_login_count INTEGER DEFAULT 0")
        if "locked_until" not in columns:
            statements.append("ALTER TABLE employee ADD COLUMN locked_until TIMESTAMP")
        if "must_change_password" not in columns:
            statements.append("ALTER TABLE employee ADD COLUMN must_change_password BOOLEAN DEFAULT 0")
        if "session_key" not in columns:
            statements.append("ALTER TABLE employee ADD COLUMN session_key VARCHAR")
        if "session_expires_at" not in columns:
            statements.append("ALTER TABLE employee ADD COLUMN session_expires_at TIMESTAMP")
        if statements:
            with engine.begin() as connection:
                for statement in statements:
                    connection.execute(text(statement))

    # photo_upload_log.employee_id 改為可空：未綁定員工的 LINE 帳號上傳也要留下記錄。
    # PostgreSQL 需 DROP NOT NULL；SQLite 不強制既有 NOT NULL 且重建表成本高，故略過。
    if "photouploadlog" in table_names:
        photo_columns = {column["name"]: column for column in inspector.get_columns("photouploadlog")}
        employee_column = photo_columns.get("employee_id")
        if (
            employee_column is not None
            and employee_column.get("nullable") is False
            and not settings.database_url.startswith("sqlite")
        ):
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE photouploadlog ALTER COLUMN employee_id DROP NOT NULL")
                )
