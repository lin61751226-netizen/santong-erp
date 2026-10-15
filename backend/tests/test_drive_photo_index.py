from copy import deepcopy
from datetime import date, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import AdminAuditLog, Employee, PhotoUploadLog, Role, Worksite
from app.services.drive_photo_index import apply_drive_index, filename_work_date, folder_id_from_url, plan_drive_index
from app.services.google_drive import FOLDER_MIME_TYPE, GoogleDriveWorklogService
from app.services.google_drive import google_drive_worklog_service as drive

ROOT = "existing-root-folder"
TODAY = date(2026, 10, 15)


def photo(id="old-photo", name="LINE_ALBUM_47-20260907_261015_1.jpg", checksum="a" * 32, **extra):
    return {"id": id, "name": name, "mimeType": "image/jpeg", "md5Checksum": checksum,
            "createdTime": "2026-10-15T13:40:00Z", "album_name": "47-1014", "parent_id": "old-date-folder", **extra}


def tree(*files):
    return {"folder_id": ROOT, "folder_name": "47", "folder_count": 2, "files": list(files or [photo()])}


@pytest.fixture
def ctx():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        actor = Employee(employee_code="ADMIN001", name="管理員", role=Role.admin, bind_token="unchanged", password_hash="same", line_user_id="same-line")
        worker = Employee(employee_code="WORKER", name="員工", role=Role.employee, bind_token="worker")
        site = Worksite(code="善捷47", name="善捷47")
        session.add_all([actor, worker, site]); session.commit()
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_actor] = lambda: actor
        with patch.object(drive, "is_configured", return_value=True), \
             patch.object(drive, "list_existing_photo_tree", AsyncMock(return_value=tree())) as scan, \
             patch.object(drive, "backup_database", AsyncMock(return_value={"status": "saved"})) as backup, \
             patch.object(drive, "_upload_bytes") as upload:
            yield {"session": session, "actor": actor, "worker": worker, "site": site, "scan": scan,
                   "backup": backup, "upload": upload, "client": TestClient(app)}
        app.dependency_overrides.clear()
    engine.dispose()


def plan(ctx, source):
    return plan_drive_index(ctx["session"], tree=source, site=ctx["site"], filename_code="47", today=TODAY)


@pytest.mark.parametrize("name,expected", [
    ("LINE_ALBUM_47-20260907_261015_27.jpg", date(2026, 9, 7)),
    ("LINE_ALBUM_47-20261014_261015_25.jpg", date(2026, 10, 14)),
])
def test_original_filename_date_not_export_date_or_folder(name, expected):
    assert filename_work_date(name, "47", TODAY) == expected


@pytest.mark.parametrize("name", ["LINE_ALBUM_76-20260907_261015_1.jpg", "LINE_ALBUM_47-2026919_261015_5.jpg",
    "LINE_ALBUM_47-2026102_261015_9.jpg", "LINE_ALBUM_47-20260230_261015_1.jpg", "LINE_ALBUM_47-20261016_261015_1.jpg", "IMG_20260907.jpg"])
def test_uncertain_dates_and_foreign_site_never_guessed(name):
    with pytest.raises(ValueError): filename_work_date(name, "47", TODAY)


def test_mixed_folder_and_same_content_deduplication(ctx):
    p = plan(ctx, tree(photo(), photo("copy", checksum="A" * 32), photo("oct", name="LINE_ALBUM_47-20261014_261015_1.jpg"),
                       photo("other", name="LINE_ALBUM_76-20260907_261015_1.jpg"), photo("bad", name="LINE_ALBUM_47-2026919_261015_1.jpg")))
    assert p["counts"] == {"new": 2, "duplicate": 1, "skipped": 2, "conflict": 0}
    assert p["dates"] == [{"work_date": "2026-09-07", "count": 1}, {"work_date": "2026-10-14", "count": 1}]
    assert apply_drive_index(ctx["session"], plan=p, actor=ctx["actor"], source_group="47&76工地照片") == 2
    assert plan(ctx, tree(photo(), photo("copy", checksum="A" * 32)))["counts"]["new"] == 0
    assert ctx["session"].exec(select(AdminAuditLog)).one().action == "drive_photo_index"
    ctx["upload"].assert_not_called()


def test_idempotent_apply_preserves_file_parent_timestamp_and_identity(ctx):
    data = {"site_id": ctx["site"].id, "filename_code": "47", "folder_url": f"https://drive.google.com/drive/folders/{ROOT}"}
    client = ctx["client"]
    preview = client.post("/api/photo-uploads/drive/preview", json=data)
    assert preview.status_code == 200 and preview.json()["counts"]["new"] == 1
    assert ctx["session"].exec(select(PhotoUploadLog)).first() is None
    result = client.post("/api/photo-uploads/drive/apply", json={**data, "digest": preview.json()["digest"]})
    assert result.status_code == 200 and result.json()["added"] == 1
    assert result.json()["backup_status"] == "saved"
    log = ctx["session"].exec(select(PhotoUploadLog)).one()
    assert log.work_date == date(2026, 9, 7) and log.album_name == "47-1014"
    assert log.drive_file_id == "old-photo" and log.drive_folder_id == "old-date-folder"
    assert log.uploaded_at == datetime(2026, 10, 15, 13, 40)
    assert "old-photo" in log.drive_url and log.source_group == "47&76工地照片"
    again = client.post("/api/photo-uploads/drive/apply", json={**data, "digest": preview.json()["digest"]})
    assert again.json()["added"] == 0
    assert len(ctx["session"].exec(select(PhotoUploadLog)).all()) == 1
    assert (ctx["actor"].password_hash, ctx["actor"].line_user_id, ctx["actor"].bind_token) == ("same", "same-line", "unchanged")
    ctx["upload"].assert_not_called()


def test_changed_files_reject_before_writing_and_failed_backup_preserves_records(ctx):
    client = ctx["client"]; data = {"site_id": ctx["site"].id, "filename_code": "47", "folder_url": ROOT}
    digest = client.post("/api/photo-uploads/drive/preview", json=data).json()["digest"]
    ctx["scan"].return_value = tree(photo("new-file"))
    assert client.post("/api/photo-uploads/drive/apply", json={**data, "digest": digest}).status_code == 409
    ctx["backup"].assert_not_awaited()
    ctx["scan"].return_value = tree()
    ctx["backup"].return_value = {"status": "failed"}
    assert client.post("/api/photo-uploads/drive/apply", json={**data, "digest": digest}).status_code == 503
    assert ctx["session"].exec(select(PhotoUploadLog)).first() is None
    ctx["backup"].side_effect = [{"status": "saved"}, {"status": "failed"}]
    result = client.post("/api/photo-uploads/drive/apply", json={**data, "digest": digest})
    assert result.json()["added"] == 1 and result.json()["backup_status"] == "failed"


def test_existing_wrong_date_is_conflict_not_overwritten(ctx):
    session = ctx["session"]
    session.add(PhotoUploadLog(employee_id=ctx["actor"].id, site_id=ctx["site"].id, file_name="keep", work_date=date(2026,10,14),
                               drive_file_id="old-photo", drive_folder_id="keep", drive_url="https://drive.google.com/file/d/old-photo/view"))
    session.commit()
    assert plan(ctx, tree())["counts"]["conflict"] == 1
    assert session.exec(select(PhotoUploadLog)).one().work_date == date(2026,10,14)


def test_permissions_and_folder_url_validation(ctx):
    assert folder_id_from_url(f"https://drive.google.com/drive/u/0/folders/{ROOT}?usp=sharing") == ROOT
    with pytest.raises(ValueError): folder_id_from_url("https://example.com/folders/private-folder")
    with pytest.raises(ValueError): folder_id_from_url("https://drive.google.com/drive/my-drive")
    data = {"site_id": ctx["site"].id, "filename_code": "47", "folder_url": ROOT}
    app.dependency_overrides[get_current_actor] = lambda: ctx["worker"]
    assert ctx["client"].post("/api/photo-uploads/drive/preview", json=data).status_code == 403
    app.dependency_overrides.pop(get_current_actor)
    assert ctx["client"].post("/api/photo-uploads/drive/preview", json=data).status_code == 401
    ctx["scan"].assert_not_awaited()


def test_drive_tree_is_paginated_metadata_only_and_skips_shortcuts():
    client = Mock(); api = client.files.return_value
    api.get.return_value.execute.return_value = {"id":ROOT,"name":"47","mimeType":FOLDER_MIME_TYPE}
    api.list.return_value.execute.side_effect = [
        {"files":[{"id":"day","name":"47-1014","mimeType":FOLDER_MIME_TYPE}],"nextPageToken":"page-two"},
        {"files":[{"id":"shortcut","name":"shortcut","mimeType":"application/vnd.google-apps.shortcut"}]},
        {"files":[{k:v for k,v in photo().items() if k not in {"parent_id","album_name"}}]},
    ]
    service = GoogleDriveWorklogService()
    with patch.object(service, "_build_client", return_value=client):
        result = service._list_existing_photo_tree_sync(ROOT)
    assert result["folder_count"] == 2 and len(result["files"]) == 2
    assert result["files"][1]["parent_id"] == "day"
    assert api.list.call_args_list[1].kwargs["pageToken"] == "page-two"
    api.create.assert_not_called(); api.update.assert_not_called(); api.get_media.assert_not_called()
