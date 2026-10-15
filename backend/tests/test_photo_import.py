from datetime import date, datetime, timedelta
from io import BytesIO
from unittest.mock import AsyncMock, Mock, patch

import httplib2
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError
from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core import db
from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import AdminAuditLog, Employee, PhotoImportJob, PhotoUploadLog, Role, Worksite
from app.services.google_drive import DriveUploadResult, GoogleDriveWorklogError, GoogleDriveWorklogService
from app.services.google_drive import google_drive_worklog_service as drive
from app.services.photo_import import MAX_PHOTO_BYTES, validate_photo


def image_bytes(color="red"):
    stream = BytesIO()
    Image.new("RGB", (4, 4), color).save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def context():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        admin = Employee(employee_code="ADMIN001", name="管理員", role=Role.admin, bind_token="keep-bind",
                         line_user_id="keep-line", password_hash="keep-password")
        worker = Employee(employee_code="EMP001", name="操作員", role=Role.employee, bind_token="worker-bind")
        sites = [Worksite(code="47", name="善捷47"), Worksite(code="76", name="金駿76"),
                 Worksite(code="old", name="停用工地", is_active=False)]
        session.add_all([admin, worker, *sites])
        session.commit()
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_actor] = lambda: admin
        uploaded = DriveUploadResult(file_id="reserved-id", file_name="stored.png", file_url="https://drive.google.com/file/d/reserved-id/view",
                                     folder_id="date-folder", date_folder_name="善捷47/2026-09/2026-09-01", content_type="image/png")
        with patch.object(drive, "is_configured", return_value=True), \
             patch.object(drive, "reserve_photo_file_id", AsyncMock(return_value="reserved-id")) as reserve, \
             patch.object(drive, "upload_album_photo", AsyncMock(return_value=uploaded)) as upload, \
             patch.object(drive, "backup_database", AsyncMock(return_value={"status": "saved"})) as backup:
            yield {"session": session, "admin": admin, "worker": worker, "sites": sites,
                   "client": TestClient(app), "upload": upload, "reserve": reserve, "backup": backup}
        app.dependency_overrides.clear()
    engine.dispose()


def send(ctx, **overrides):
    data = {"site_id": str(ctx["sites"][0].id), "work_date": "2026-09-01",
            "source_group": "47&76工地照片", "album_name": "九月施工"}
    content = overrides.pop("content", image_bytes())
    data.update(overrides)
    return ctx["client"].post("/api/photo-uploads/import", data=data,
                              files={"file": ("../../原相片.png", content, "image/png")})


def test_import_preserves_dates_source_audit_and_identity(context):
    assert send(context).json()["status"] == "imported"
    session = context["session"]
    log = session.exec(select(PhotoUploadLog)).one()
    assert log.work_date == date(2026, 9, 1)
    assert log.uploaded_at > datetime(2026, 9, 1)
    assert log.original_file_name == "原相片.png"
    assert log.source_group == "47&76工地照片" and log.album_name == "九月施工"
    assert log.line_user_id is None and log.assignment_id is None
    assert session.exec(select(AdminAuditLog)).one().action == "photo_album_import"
    admin = session.get(Employee, context["admin"].id)
    assert (admin.line_user_id, admin.bind_token, admin.password_hash) == ("keep-line", "keep-bind", "keep-password")
    context["backup"].assert_awaited_once()


def test_duplicate_ignores_filename_and_keeps_different_dates_sites(context):
    assert send(context).json()["status"] == "imported"
    assert send(context, album_name="另一相簿").json()["status"] == "duplicate"
    assert context["upload"].await_count == 1
    assert send(context, site_id=str(context["sites"][1].id)).json()["status"] == "imported"
    assert send(context, work_date="2026-09-02").json()["status"] == "imported"
    assert context["upload"].await_count == 3


def test_failed_upload_can_retry_same_reserved_id(context):
    good = context["upload"].return_value
    context["upload"].side_effect = [GoogleDriveWorklogError("Google Drive 授權失效"), good]
    assert send(context).status_code == 502
    assert context["session"].exec(select(PhotoUploadLog)).first() is None
    assert send(context).json()["status"] == "imported"
    assert context["reserve"].await_count == 1
    assert [call.kwargs["file_id"] for call in context["upload"].await_args_list] == ["reserved-id", "reserved-id"]


def test_busy_and_stale_jobs(context):
    send(context)
    session = context["session"]
    log = session.exec(select(PhotoUploadLog)).one()
    session.delete(log)
    job = session.exec(select(PhotoImportJob)).one()
    job.status = "processing"
    session.add(job)
    session.commit()
    assert send(context).status_code == 409
    assert context["upload"].await_count == 1
    job.updated_at = datetime.utcnow() - timedelta(minutes=16)
    session.add(job)
    session.commit()
    assert send(context).json()["status"] == "imported"
    assert context["reserve"].await_count == 1


@pytest.mark.parametrize("changes,status_code", [
    ({"site_id": "9999"}, 400), ({"site_id": "3"}, 400),
    ({"work_date": "2099-01-01"}, 400), ({"work_date": "not-date"}, 422),
    ({"content": b"not an image"}, 400), ({"content": b""}, 413),
    ({"source_group": "x" * 121}, 422),
])
def test_invalid_input_does_not_upload(context, changes, status_code):
    assert send(context, **changes).status_code == status_code
    context["upload"].assert_not_awaited()
    context["reserve"].assert_not_awaited()


def test_size_and_real_image_type():
    with pytest.raises(ValueError, match="10 MB"):
        validate_photo(b"x" * (MAX_PHOTO_BYTES + 1))
    assert validate_photo(image_bytes()) == ("image/png", ".png")


def test_permissions_and_no_configuration(context):
    app.dependency_overrides[get_current_actor] = lambda: context["worker"]
    assert send(context).status_code == 403
    app.dependency_overrides.pop(get_current_actor)
    assert send(context).status_code == 401
    app.dependency_overrides[get_current_actor] = lambda: context["admin"]
    with patch.object(drive, "is_configured", return_value=False):
        assert send(context).status_code == 503
    context["upload"].assert_not_awaited()


def test_backup_failure_is_separate_and_duplicate_retry_repairs_backup(context):
    context["backup"].side_effect = RuntimeError("private backup error")
    result = send(context)
    assert result.status_code == 200 and result.json()["backup_status"] == "failed"
    context["backup"].side_effect = None
    assert send(context).json() == {"status": "duplicate", "id": 1,
                                  "drive_url": context["upload"].return_value.file_url, "backup_status": "saved"}
    assert context["upload"].await_count == 1


def test_original_date_filters_and_work_journal(context):
    send(context)
    session = context["session"]
    session.add(PhotoUploadLog(employee_id=context["admin"].id, site_id=context["sites"][0].id,
                              file_name="old.png", drive_file_id="old", drive_folder_id="old-folder", drive_url="https://drive.google.com/file/d/old/view",
                              uploaded_at=datetime(2026, 8, 31, 16, 30)))
    session.commit()
    client = context["client"]
    logs = client.get("/api/photo-uploads?date_from=2026-09-01&date_to=2026-09-01").json()
    assert len(logs) == 2 and all(item["work_date"] == "2026-09-01" for item in logs)
    journal = client.get("/api/worksite-journals?target_date=2026-09-01").json()
    photos = journal["sites"][0]["photos"]
    assert len(photos) == 2
    assert photos[1]["source_group"] == "47&76工地照片"
    assert "匯入人" in photos[1]["employee_name"]
    today = date.today().isoformat()
    assert client.get(f"/api/worksite-journals?target_date={today}").json()["sites"] == []
    from app.services.ai_ops import active_sites_for_day, gather_site_day
    assert gather_site_day(session, date(2026, 9, 1), context["sites"][0].id)["photo_total"] == 2
    assert active_sites_for_day(session, date(2026, 9, 1))[0] == [context["sites"][0].id]
    assert active_sites_for_day(session, date.today())[0] == []


def test_legacy_migration_is_repeatable_and_non_destructive():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE photouploadlog (id INTEGER PRIMARY KEY, employee_id INTEGER, file_name VARCHAR)"))
        connection.execute(text("INSERT INTO photouploadlog VALUES (1,7,'keep.jpg')"))
    with patch.object(db, "engine", engine):
        db._apply_lightweight_migrations()
        db._apply_lightweight_migrations()
    columns = {column["name"] for column in inspect(engine).get_columns("photouploadlog")}
    assert {"work_date", "import_key", "original_file_name", "source_group", "album_name"} <= columns
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id,employee_id,file_name,work_date FROM photouploadlog")).one() == (1, 7, "keep.jpg", None)
    engine.dispose()


def test_drive_import_uses_original_day_private_and_stable_id():
    service = GoogleDriveWorklogService()
    with patch.object(service, "is_configured", return_value=True), patch.object(service, "_upload_bytes") as upload:
        import asyncio
        asyncio.run(service.upload_album_photo(content=b"png", content_type="image/png", extension=".png",
                                               work_date=date(2026, 9, 1), site_name="善捷47", import_key="hash", file_id="stable"))
    assert upload.call_args.kwargs["folder_path"] == ("善捷47", "2026-09", "2026-09-01")
    assert upload.call_args.kwargs["public_share"] is False
    assert upload.call_args.kwargs["file_id"] == "stable"


def test_drive_recovers_successful_create_after_timeout_without_duplicate():
    service = GoogleDriveWorklogService()
    client = Mock()
    missing = HttpError(httplib2.Response({"status": "404"}), b'{}')
    client.files.return_value.get.return_value.execute.side_effect = [missing, {
        "id": "stable", "name": "import.png", "parents": ["day-folder"], "webViewLink": "https://drive.google.com/file/d/stable/view",
    }]
    client.files.return_value.create.return_value.execute.side_effect = TimeoutError("response lost")
    with patch.object(service, "_build_client", return_value=client), \
         patch.object(service, "_find_or_create_photo_folder", return_value="day-folder"), \
         patch("app.services.google_drive.time.sleep"):
        result = service._upload_bytes(file_name="import.png", content=b"png", content_type="image/png",
                                       folder_name="site/month/day", folder_path=("site", "month", "day"), public_share=False, file_id="stable")
    assert result.file_id == "stable"
    client.files.return_value.create.assert_called_once()
    client.permissions.assert_not_called()
