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
