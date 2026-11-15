import hashlib
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import Employee, FinanceEntry, ManagedDocument, Role, WorkbookArchive
from app.routes import workbook_archive as archive_routes
from test_finance_imports import _annual_workbook_bytes, _liuhe_workbook_bytes


def test_archive_three_sources_preserves_bytes_and_does_not_book_money():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    actor = Employee(employee_code="ADMIN001", name="管理員", bind_token="admin", role=Role.admin)
    session.add(actor)
    session.flush()
    contents = [_annual_workbook_bytes(), _annual_workbook_bytes(), _liuhe_workbook_bytes()]
    files = [
        ("全年管理版_線上編輯.xlsx", "forklift", "primary"),
        ("全年管理版.xlsx", "forklift", "reference"),
        ("三通公司收支明細表_六和.xlsx", "liuhe", "independent"),
    ]
    docs = []
    for i, (name, _, _) in enumerate(files):
        doc = ManagedDocument(category="收支明細", title=name, original_file_name=name,
                              stored_file_name=name, drive_file_id=f"file-{i}",
                              drive_folder_id="folder", drive_url="https://example.test/file",
                              uploaded_by_id=actor.id)
        session.add(doc)
        docs.append(doc)
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_actor] = lambda: actor
    download = AsyncMock(side_effect=lambda file_id: contents[int(file_id.split("-")[1])])
    backup = AsyncMock(return_value={"status": "saved"})
    try:
        with patch.object(archive_routes.drive, "download_file_bytes", download), \
             patch.object(archive_routes.drive, "backup_database", backup):
            client = TestClient(app)
            payload = {"items": [{"document_id": doc.id,
                                  "expected_content_sha256": hashlib.sha256(contents[i]).hexdigest(),
                                  "ledger_scope": files[i][1], "source_role": files[i][2]}
                                 for i, doc in enumerate(docs)]}
            response = client.post("/api/workbook-archive", json=payload)
            assert response.status_code == 200, response.text
            assert response.json() == {"saved": 3, "replayed": 0, "backup": "saved"}
            assert backup.await_count == 2
            assert session.exec(select(FinanceEntry)).all() == []
            rows = session.exec(select(WorkbookArchive).order_by(WorkbookArchive.id)).all()
            assert [row.content for row in rows] == contents
            assert [(row.ledger_scope, row.source_role) for row in rows] == [
                ("forklift", "primary"), ("forklift", "reference"), ("liuhe", "independent")]
            assert client.get(f"/api/workbook-archive/{rows[0].id}/download").content == contents[0]
            replay = client.post("/api/workbook-archive", json=payload)
            assert replay.json()["replayed"] == 3
            assert backup.await_count == 2
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def test_archive_rejects_changed_file_before_backup():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    actor = Employee(employee_code="ADMIN001", name="管理員", bind_token="admin", role=Role.admin)
    session.add(actor)
    session.flush()
    doc = ManagedDocument(category="收支明細", title="來源", original_file_name="全年管理版.xlsx",
                          stored_file_name="source.xlsx", drive_file_id="file", drive_folder_id="folder",
                          drive_url="https://example.test/file", uploaded_by_id=actor.id)
    session.add(doc)
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_actor] = lambda: actor
    backup = AsyncMock(return_value={"status": "saved"})
    try:
        with patch.object(archive_routes.drive, "download_file_bytes", AsyncMock(return_value=_annual_workbook_bytes())), \
             patch.object(archive_routes.drive, "backup_database", backup):
            response = TestClient(app).post("/api/workbook-archive", json={"items": [{
                "document_id": doc.id, "expected_content_sha256": "0" * 64,
                "ledger_scope": "forklift", "source_role": "primary"}]})
            assert response.status_code == 409
            assert session.exec(select(WorkbookArchive)).all() == []
            backup.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()
