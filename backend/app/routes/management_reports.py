"""Owner/admin-only monthly and annual reconciliation reports."""

import asyncio
import calendar
import csv
import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.db import get_session
from app.deps import require_roles
from app.models import AdminAuditLog, Employee, ManagedDocument, ManagementWorkbookEdit, ManagementWorkbookSnapshot, Role
from app.services.google_drive import google_drive_worklog_service as drive
from app.services.management_reports import PAY_FIELDS, analyze_workbook, employee_matches, report


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


class MonthGridSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: int = Field(gt=0)
    kind: Literal["payroll", "roster"]
    month: str
    source: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=0)
    request_id: str = Field(min_length=8, max_length=100)
    values: dict
    preview_only: bool = False
    confirmed: bool = False


def month_grid_source(session: Session, snapshot_id: int, kind: str, month: str, source: str | None = None):
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "請選擇有效月份")
    snapshot = session.get(ManagementWorkbookSnapshot, snapshot_id)
    if snapshot is None:
        raise HTTPException(404, "找不到已保存的全年管理版本")
    rows = [item for item in snapshot.data[kind] if item.get("month") == month]
    if source is not None and not any(item["source"] == source for item in rows):
        raise HTTPException(404, "找不到此月份的來源列")
    return snapshot, rows


def normalized_month_values(kind: str, month: str, values: dict) -> dict:
    if kind == "payroll":
        keys = set(PAY_FIELDS) | {"status", "note"}
        if set(values) != keys:
            raise HTTPException(422, "薪資欄位不完整或含未允許的欄位")
        normalized = {}
        for key in PAY_FIELDS:
            value = values[key]
            if value in ("", None):
                normalized[key] = None
                continue
            try:
                amount = Decimal(str(value))
            except InvalidOperation as exc:
                raise HTTPException(422, f"{key} 不是有效金額") from exc
            if not amount.is_finite() or amount < 0 or amount > 1_000_000_000 or amount.as_tuple().exponent < -2:
                raise HTTPException(422, f"{key} 請填 0 至十億元、最多兩位小數")
            normalized[key] = float(amount)
        for key in ("status", "note"):
            value = values[key]
            if not isinstance(value, str) or len(value) > (100 if key == "status" else 2000):
                raise HTTPException(422, f"{key} 格式不正確")
            normalized[key] = value.strip()
        return normalized
    if set(values) != {"marks", "note"} or not isinstance(values["marks"], dict):
        raise HTTPException(422, "排休欄位不完整")
    days = calendar.monthrange(int(month[:4]), int(month[5:]))[1]
    marks = {}
    for key, value in values["marks"].items():
        if not str(key).isdigit() or not 1 <= int(key) <= days or not isinstance(value, str) or len(value) > 20:
            raise HTTPException(422, "排休日期或內容不正確")
        if value.strip():
            marks[str(int(key))] = value.strip()
    note = values["note"]
    if not isinstance(note, str) or len(note) > 2000:
        raise HTTPException(422, "排休備註格式不正確")
    return {"marks": marks, "note": note.strip()}


def month_row_values(item: dict, kind: str) -> dict:
    if kind == "payroll":
        return {key: item.get(key) for key in PAY_FIELDS} | {
            "status": item.get("status", ""), "note": item.get("note", ""),
        }
    return {"marks": item.get("marks", {}), "note": item.get("note", "")}


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


@router.get("/month-grid")
def read_month_grid(
    snapshot_id: int = Query(..., gt=0), month: str = Query(...),
    kind: Literal["payroll", "roster"] = Query(...),
    session: Session = Depends(get_session), actor: Employee = Depends(admin),
):
    snapshot, rows = month_grid_source(session, snapshot_id, kind, month)
    edits = {item.source: item for item in session.exec(select(ManagementWorkbookEdit).where(
        ManagementWorkbookEdit.snapshot_id == snapshot.id,
        ManagementWorkbookEdit.kind == kind,
    )).all()}
    return {
        "snapshot_id": snapshot.id, "month": month, "kind": kind,
        "source_document_id": snapshot.source_document_id,
        "fields": PAY_FIELDS if kind == "payroll" else list(range(1, calendar.monthrange(int(month[:4]), int(month[5:]))[1] + 1)),
        "rows": [{
            "source": item["source"], "name": item["name"],
            "employee_code": item.get("employee_code", ""),
            "values": edits[item["source"]].values if item["source"] in edits else month_row_values(item, kind),
            "revision": edits[item["source"]].revision if item["source"] in edits else 0,
        } for item in rows],
    }


@router.post("/month-grid")
async def save_month_grid(
    payload: MonthGridSave,
    session: Session = Depends(get_session), actor: Employee = Depends(admin),
):
    _, rows = month_grid_source(session, payload.snapshot_id, payload.kind, payload.month, payload.source)
    base = next(item for item in rows if item["source"] == payload.source)
    normalized = normalized_month_values(payload.kind, payload.month, payload.values)
    edit = session.exec(select(ManagementWorkbookEdit).where(
        ManagementWorkbookEdit.snapshot_id == payload.snapshot_id,
        ManagementWorkbookEdit.kind == payload.kind,
        ManagementWorkbookEdit.source == payload.source,
    )).first()
    if edit and edit.last_request_id == payload.request_id:
        if edit.values != normalized:
            raise HTTPException(409, "同一存檔識別碼的內容不同")
        return {"saved": True, "replayed": True, "revision": edit.revision, "backup": await backup()}
    revision = edit.revision if edit else 0
    if revision != payload.expected_revision:
        raise HTTPException(409, "資料已由其他人更新，請重新載入後核對")
    original = month_row_values(base, payload.kind)
    before = edit.values if edit else original
    if payload.preview_only:
        return {"saved": False, "changed": before != normalized, "before": before, "after": normalized,
                "revision": revision, "message": "僅預覽，未寫入正式資料"}
    if not payload.confirmed:
        raise HTTPException(409, "請先預覽並確認此列變更")
    if before == normalized:
        return {"saved": True, "unchanged": True, "revision": revision, "backup": await backup()}
    if edit:
        result = session.execute(update(ManagementWorkbookEdit).where(
            ManagementWorkbookEdit.id == edit.id,
            ManagementWorkbookEdit.revision == revision,
        ).values(values=normalized, revision=revision + 1, last_request_id=payload.request_id,
                 updated_by_id=actor.id, updated_at=datetime.utcnow()).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            session.rollback()
            raise HTTPException(409, "資料已更新，請重新載入")
    else:
        session.add(ManagementWorkbookEdit(
            snapshot_id=payload.snapshot_id, kind=payload.kind, source=payload.source,
            values=normalized, last_request_id=payload.request_id, updated_by_id=actor.id,
        ))
    session.add(AdminAuditLog(
        actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
        action="management_month_edit", entity_type="management_workbook", entity_id=payload.snapshot_id,
        summary=json.dumps({"month": payload.month, "kind": payload.kind, "source": payload.source,
                            "before": before, "after": normalized}, ensure_ascii=False),
    ))
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "資料已由其他人更新，請重新載入") from exc
    return {"saved": True, "revision": revision + 1, "backup": await backup()}


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
