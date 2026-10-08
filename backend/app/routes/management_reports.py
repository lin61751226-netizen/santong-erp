"""Owner/admin-only monthly and annual reconciliation reports."""

import asyncio
import csv
import hashlib
import json
import re
from io import StringIO
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.db import get_session
from app.deps import require_roles
from app.models import AdminAuditLog, Employee, ManagedDocument, ManagementWorkbookSnapshot, Role
from app.services.google_drive import google_drive_worklog_service as drive
from app.services.management_reports import analyze_workbook, employee_matches, report


def private_response(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/api/management-reports", tags=["management-reports"], dependencies=[Depends(private_response)])
admin = require_roles(Role.owner, Role.admin)
Kind = Literal["finance", "payroll", "roster", "calendar", "annual", "categories", "contacts"]


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: int = Field(gt=0)
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    employee_mapping: dict[str, str | None] = Field(default_factory=dict)


def source_document(session, document_id):
    document = session.get(ManagedDocument, document_id)
    if document is None:
        raise HTTPException(404, "找不到來源文件")
    if document.category != "通訊錄與年度管理" or not document.original_file_name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "請選擇通訊錄與全年管理 Excel")
    return document


async def analyze(document):
    try:
        content = await drive.download_file_bytes(document.drive_file_id)
        return await asyncio.to_thread(analyze_workbook, content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "讀取來源 Excel 失敗；未寫入資料，請確認 Drive 授權") from exc


async def backup():
    try:
        result = await drive.backup_database()
        return {"status": result.get("status", "failed")}
    except Exception:
        return {"status": "failed"}


@router.get("/sources")
def sources(session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    snapshots = session.exec(select(ManagementWorkbookSnapshot).order_by(ManagementWorkbookSnapshot.id.desc())).all()
    documents = session.exec(select(ManagedDocument).where(ManagedDocument.category == "通訊錄與年度管理").order_by(ManagedDocument.id.desc())).all()
    return [{"id": doc.id, "name": doc.original_file_name,
             "versions": [{"id": row.id, "created_at": row.created_at.isoformat(), "months": sorted({item["month"] for kind in ("finance", "payroll", "roster", "calendar") for item in row.data[kind]})}
                          for row in snapshots if row.source_document_id == doc.id]} for doc in documents]


@router.get("/preview")
async def preview(document_id: int, session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    data = await analyze(source_document(session, document_id))
    employees = session.exec(select(Employee).order_by(Employee.employee_code)).all()
    mapping, matches = employee_matches(data, employees)
    return {"content_sha256": data["content_sha256"], "counts": {kind: len(data[kind]) for kind in ("finance", "payroll", "roster", "calendar", "contacts")},
            "warnings": data["warnings"], "mapping": mapping, "matches": matches,
            "employees": [{"code": item.employee_code, "name": item.name} for item in employees],
            "months": sorted({item["month"] for kind in ("finance", "payroll", "roster", "calendar") for item in data[kind]})}


@router.post("/import")
async def import_snapshot(payload: ImportRequest, session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    document = source_document(session, payload.document_id)
    data = await analyze(document)
    if data["content_sha256"] != payload.expected_content_sha256:
        raise HTTPException(409, "Excel已變更，請重新預覽；本次未保存")
    employees = session.exec(select(Employee)).all()
    names = {item["name"] for kind in ("payroll", "roster") for item in data[kind]}
    codes = {item.employee_code for item in employees}
    if set(payload.employee_mapping) - names or any(code and code not in codes for code in payload.employee_mapping.values()):
        raise HTTPException(422, "員工對應不存在，請重新預覽")
    mapping, _ = employee_matches(data, employees, payload.employee_mapping)
    # Explicitly choosing 'unmatched' must not fall back to an automatic match.
    mapping.update(payload.employee_mapping)
    version_key = hashlib.sha256(json.dumps([document.id, data["content_sha256"], mapping], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    prior = session.exec(select(ManagementWorkbookSnapshot).where(ManagementWorkbookSnapshot.version_key == version_key)).first()
    if prior:
        return {"saved": True, "replayed": True, "snapshot_id": prior.id, "backup": await backup()}
    snapshot = ManagementWorkbookSnapshot(version_key=version_key, source_document_id=document.id,
        content_sha256=data["content_sha256"], employee_mapping=mapping, data=data, created_by_id=actor.id)
    try:
        session.add(snapshot)
        session.flush()
        session.add(AdminAuditLog(actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
            action="import", entity_type="management_workbook", entity_id=snapshot.id,
            summary=f"保存全年管理對照版本：{document.original_file_name}；不改正式帳/員工/請假/打卡"))
        session.commit()
        session.refresh(snapshot)
    except IntegrityError:
        session.rollback()
        prior = session.exec(select(ManagementWorkbookSnapshot).where(ManagementWorkbookSnapshot.version_key == version_key)).first()
        if not prior:
            raise HTTPException(409, "版本保存衝突，請重新載入")
        snapshot = prior
    return {"saved": True, "snapshot_id": snapshot.id, "backup": await backup()}


def get_report(session, snapshot_id, month, kind):
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "請選擇有效西元年月，例如2026-09")
    snapshot = session.get(ManagementWorkbookSnapshot, snapshot_id) if snapshot_id else None
    if snapshot_id and snapshot is None:
        raise HTTPException(404, "找不到已存Excel版本")
    return report(session, snapshot, month, kind)


@router.get("/report")
def read_report(month: str, kind: Kind = "finance", snapshot_id: int | None = Query(None, gt=0),
                offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    data = get_report(session, snapshot_id, month, kind)
    data["total"] = len(data["rows"])
    data["rows"] = data["rows"][offset:offset + limit]
    data["offset"] = offset
    return data


@router.get("/export")
async def export_report(month: str, kind: Kind = "finance", snapshot_id: int | None = Query(None, gt=0),
                  session: Session = Depends(get_session), actor: Employee = Depends(admin)):
    data = get_report(session, snapshot_id, month, kind)
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    def literal(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return "待確認" if value is None else value
    writer.writerow(data["fields"])
    writer.writerows([literal(value) for value in row] for row in data["rows"])
    session.add(AdminAuditLog(actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
        action="export", entity_type="management_report", entity_id=snapshot_id,
        summary=f"匯出{month} {data['title']}，{len(data['rows'])}列"))
    session.commit()
    backup_status = (await backup())["status"]
    return Response(stream.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(month + '_' + data['title'] + '.csv')}", "Cache-Control": "no-store", "X-Database-Backup": backup_status})
