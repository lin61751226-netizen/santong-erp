import logging
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session

from app.core.db import get_session
from app.core.local_time import local_today
from app.deps import require_roles
from app.models import Employee, Role, Worksite
from app.services.google_drive import GoogleDriveWorklogError, google_drive_worklog_service as drive
from app.services.photo_import import MAX_PHOTO_BYTES, PhotoImportError, import_photo

router = APIRouter(prefix="/api/photo-uploads", tags=["photo-import"])
logger = logging.getLogger(__name__)


@router.post("/import")
async def import_album_photo(
    site_id: int = Form(...), work_date: date = Form(...),
    source_group: str = Form(default="47&76工地照片", max_length=120),
    album_name: str = Form(default="", max_length=120),
    file: UploadFile = File(...), session: Session = Depends(get_session),
    actor: Employee = Depends(require_roles(Role.owner, Role.admin)),
):
    site = session.get(Worksite, site_id)
    if not site or not site.is_active:
        raise HTTPException(400, "請選擇有效工地；不同工地請分批匯入")
    if work_date > local_today():
        raise HTTPException(400, "原施工日期不可晚於今天")
    if not drive.is_configured():
        raise HTTPException(503, "Google Drive 尚未設定，請管理員確認授權")
    try:
        content = await file.read(MAX_PHOTO_BYTES + 1)
        result = await import_photo(
            session, actor=actor, site=site, work_date=work_date, content=content,
            file_name=file.filename or "照片", source_group=source_group.strip(), album_name=album_name.strip(),
        )
    except PhotoImportError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except GoogleDriveWorklogError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        logger.exception("Album photo import failed")
        raise HTTPException(502, "匯入未完成，請重試同一照片；系統將沿用既有檔案編號避免重複") from exc
    finally:
        await file.close()
    # Local commit and off-host durability are separate outcomes, not a fake
    # upload failure that would encourage the user to upload everything again.
    try:
        backup = await drive.backup_database()
        result["backup_status"] = backup.get("status", "failed")
    except Exception:
        logger.exception("Album photo import database backup failed")
        result["backup_status"] = "failed"
    return result
