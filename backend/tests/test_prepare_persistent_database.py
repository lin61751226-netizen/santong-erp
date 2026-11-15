import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

from tools.prepare_persistent_database import inspect_database, prepare_persistent_database


def make_database(path: Path, employee_rows: int = 1) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE employee (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("CREATE TABLE attendanceevent (id INTEGER PRIMARY KEY)")
        connection.executemany("INSERT INTO employee (name) VALUES (?)", [("test",)] * employee_rows)
        connection.execute("INSERT INTO attendanceevent DEFAULT VALUES")
        connection.commit()


def test_prepares_new_database_and_preserves_records() -> None:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.sqlite3"
        target = Path(directory) / "persistent.sqlite3"
        make_database(source)
        result = prepare_persistent_database(source, target)
        assert result["status"] == "prepared"
        assert result["counts"]["employee"] == 1
        assert inspect_database(target) == inspect_database(source)
        with closing(sqlite3.connect(target)) as connection:
            connection.execute("INSERT INTO attendanceevent DEFAULT VALUES")
            connection.commit()
        assert inspect_database(target)["attendanceevent"] == 2
        assert inspect_database(source)["attendanceevent"] == 1


def test_never_overwrites_existing_target() -> None:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.sqlite3"
        target = Path(directory) / "persistent.sqlite3"
        make_database(source)
        target.write_bytes(b"existing data")
        with pytest.raises(FileExistsError):
            prepare_persistent_database(source, target)
        assert target.read_bytes() == b"existing data"


@pytest.mark.parametrize("source_state", ["missing", "invalid", "empty_employees"])
def test_rejects_invalid_source_without_creating_target(source_state: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.sqlite3"
        target = Path(directory) / "persistent.sqlite3"
        if source_state == "invalid":
            source.write_bytes(b"not SQLite")
        elif source_state == "empty_employees":
            make_database(source, employee_rows=0)
        with pytest.raises(ValueError):
            prepare_persistent_database(source, target)
        assert not target.exists()
