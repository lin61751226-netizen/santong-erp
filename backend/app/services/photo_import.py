"""Historical album imports without changing LINE event timestamps or bindings."""
from __future__ import annotations

import hashlib
import io
import warnings
from datetime import date, datetime, timedelta

from PIL import Image, UnidentifiedImageError
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import AdminAuditLog, Employee, PhotoImportJob, PhotoUploadLog, Worksite
from app.services.google_drive import google_drive_worklog_service as drive

MAX_PHOTO_BYTES = 10 * 1024 * 1024
IMAGE_TYPES = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}


class PhotoImportError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def validate_photo(content: bytes) -> tuple[str, str]:
    if not content or len(content) > MAX_PHOTO_BYTES:
        raise PhotoImportError("照片不可空白，且每張最多 10 MB", 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                image_type = IMAGE_TYPES.get(image.format)
                if not image_type or image.width * image.height > 40_000_000:
                    raise PhotoImportError("僅支援 JPG、PNG、WebP，解析度最多 4,000 萬畫素")
                image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise PhotoImportError("照片格式損壞或不支援，請改用 JPG、PNG、WebP 原圖") from exc
    return image_type


async def import_photo(
    session: Session, *, actor: Employee, site: Worksite, work_date: date,
    content: bytes, file_name: str, source_group: str, album_name: str,
) -> dict:
    content_type, extension = validate_photo(content)
    original_name = file_name.replace("\\", "/").split("/")[-1].strip()[:180] or "照片"
    digest = hashlib.sha256(content).hexdigest()
    import_key = hashlib.sha256(f"{site.id}:{work_date}:{digest}".encode()).hexdigest()
    existing = session.exec(select(PhotoUploadLog).where(PhotoUploadLog.import_key == import_key)).first()
    if existing:
        return {"status": "duplicate", "id": existing.id, "drive_url": existing.drive_url}

    now = datetime.utcnow()
    job = session.exec(select(PhotoImportJob).where(PhotoImportJob.import_key == import_key)).first()
    if not job:
        job = PhotoImportJob(import_key=import_key, updated_at=now)
        session.add(job)
        try:
            session.commit()
            session.refresh(job)
        except IntegrityError as exc:
            session.rollback()
            raise PhotoImportError("同一照片正在匯入，請稍後重試", 409) from exc
    else:
        # A DB compare-and-set protects against two browser tabs/processes claiming
        # the same failed or interrupted upload simultaneously.
        claimed = session.execute(update(PhotoImportJob).where(
            PhotoImportJob.id == job.id,
            or_(PhotoImportJob.status == "failed", PhotoImportJob.updated_at < now - timedelta(minutes=15)),
        ).values(status="processing", updated_at=now).execution_options(synchronize_session=False))
        session.commit()
        if claimed.rowcount != 1:
            raise PhotoImportError("同一照片正在匯入，請稍後重試；中斷作業可於 15 分鐘後恢復", 409)
        session.refresh(job)
    try:
        if not job.drive_file_id:
            job.drive_file_id = await drive.reserve_photo_file_id()
            session.add(job)
            session.commit()
        uploaded = await drive.upload_album_photo(
            content=content, content_type=content_type, extension=extension,
            work_date=work_date, site_name=site.name, import_key=import_key, file_id=job.drive_file_id,
        )
        log = PhotoUploadLog(
            employee_id=actor.id, site_id=site.id, work_date=work_date,
            original_file_name=original_name, source_group=source_group or None,
            album_name=album_name or None, import_key=import_key,
            file_name=uploaded.file_name, drive_file_id=uploaded.file_id,
            drive_folder_id=uploaded.folder_id, drive_url=uploaded.file_url,
            note="相簿批次匯入；員工欄為匯入人，非原拍攝者",
        )
        session.add(log)
        session.flush()
        session.add(AdminAuditLog(
            actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
            action="photo_album_import", entity_type="photo_upload", entity_id=log.id,
            summary=f"相簿匯入：{site.name}｜{work_date}｜{source_group}｜{album_name}｜{original_name}",
        ))
        job.status = "done"
        job.updated_at = datetime.utcnow()
        session.add(job)
        session.commit()
        return {"status": "imported", "id": log.id, "drive_url": log.drive_url,
                "folder_path": uploaded.date_folder_name}
    except Exception:
        session.rollback()
        session.refresh(job)
        job.status = "failed"
        job.updated_at = datetime.utcnow()
        session.add(job)
        session.commit()
        raise
