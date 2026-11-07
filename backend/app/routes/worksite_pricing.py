"""Separate, review-first worksite pricing; never modifies the legacy Excel tariff."""

import calendar
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.db import get_session
from app.deps import require_roles
from app.models import AdminAuditLog, Employee, Role, Worksite, WorksiteForkliftCharge, WorksiteForkliftRate
from app.services.google_drive import google_drive_worklog_service as drive


router = APIRouter(prefix="/api/worksite-pricing", tags=["worksite-pricing"])
admin = require_roles(Role.owner, Role.admin, Role.accounting)


class ChargeRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worksite_id: int = Field(gt=0)
    work_date: date
    vehicle_type: str = Field(min_length=1, max_length=100)
    units: int = Field(ge=0, le=100)
    normal_hours: float = Field(ge=0, le=8)
    overtime_hours: float = Field(ge=0, le=24)
    holiday_reason: Literal["", "週日", "元旦", "春節"] = ""
    external_customer: str = Field(default="", max_length=200)
    fuel_cost: float = Field(ge=0, le=1_000_000_000)
    payer: str = Field(default="", max_length=200)
    manual_total_amount: float | None = Field(default=None, ge=0, le=1_000_000_000, allow_inf_nan=False)
    manual_amount_reason: str = Field(default="", max_length=200)
    expected_revision: int = Field(ge=0)
    request_id: str = Field(min_length=8, max_length=100)
    preview_only: bool = False
    confirmed: bool = False


def _rates(session: Session, site_id: int):
    site = session.get(Worksite, site_id)
    if site is None:
        raise HTTPException(404, "找不到工地")
    rates = session.exec(select(WorksiteForkliftRate).where(
        WorksiteForkliftRate.worksite_id == site_id,
    ).order_by(WorksiteForkliftRate.vehicle_type)).all()
    if not rates:
        raise HTTPException(404, "此工地尚無堆高機計價單")
    return site, rates


def _required_holiday(day: date) -> str:
    if day.weekday() == 6:
        return "週日"
    if day.month == 1 and day.day == 1:
        return "元旦"
    return ""


def _amount(rate: WorksiteForkliftRate, units: int, normal: float, overtime: float, holiday: bool) -> tuple[float, float]:
    hourly = rate.holiday_hourly if holiday else rate.weekday_hourly
    daily = rate.holiday_daily if holiday else rate.weekday_daily
    base = Decimal(daily) if normal == 8 else Decimal(str(normal)) * Decimal(hourly)
    normal_amount = Decimal(units) * base
    overtime_amount = Decimal(units) * Decimal(str(overtime)) * Decimal(rate.overtime_hourly)
    return float(normal_amount), float(overtime_amount)


def _stored_values(item: WorksiteForkliftCharge | None) -> dict:
    return {
        "units": item.units if item else 0,
        "normal_hours": item.normal_hours if item else 0,
        "overtime_hours": item.overtime_hours if item else 0,
        "holiday_reason": item.holiday_reason if item else "",
        "external_customer": (item.external_customer or "") if item else "",
        "fuel_cost": item.fuel_cost if item else 0,
        "payer": (item.payer or "") if item else "",
        "manual_total_amount": item.manual_total_amount if item else None,
        "manual_amount_reason": (item.manual_amount_reason or "") if item else "",
    }


async def _backup():
    try:
        result = await drive.backup_database()
        return {"status": result.get("status", "failed")}
    except Exception:
        return {"status": "failed"}


@router.get("/month")
def read_month(
    worksite_id: int = Query(..., gt=0), year: int = Query(..., ge=2020, le=2200),
    month: int = Query(..., ge=1, le=12),
    session: Session = Depends(get_session), actor: Employee = Depends(admin),
):
    site, rates = _rates(session, worksite_id)
    first = date(year, month, 1)
    if first < min(rate.effective_from for rate in rates).replace(day=1):
        raise HTTPException(422, "國泰慈文自115年11月開始計價；之前的月份不可登錄")
    last = date(year, month, calendar.monthrange(year, month)[1])
    charges = session.exec(select(WorksiteForkliftCharge).where(
        WorksiteForkliftCharge.worksite_id == site.id,
        WorksiteForkliftCharge.work_date >= first,
        WorksiteForkliftCharge.work_date <= last,
    )).all()
    by_key = {(item.work_date, item.vehicle_type): item for item in charges}
    sheets = []
    for rate in rates:
        rows = []
        for number in range(1, last.day + 1):
            day = date(year, month, number)
            if day < rate.effective_from:
                continue
            item = by_key.get((day, rate.vehicle_type))
            values = _stored_values(item)
            if not item:
                values["holiday_reason"] = _required_holiday(day)
            normal_amount, overtime_amount = _amount(rate, values["units"], values["normal_hours"],
                                                      values["overtime_hours"], bool(values["holiday_reason"]))
            calculated_total = normal_amount + overtime_amount
            rows.append({"day": day.isoformat(), "revision": item.revision if item else 0,
                         **values, "normal_amount": normal_amount,
                         "overtime_amount": overtime_amount, "calculated_total": calculated_total,
                         "total_amount": values["manual_total_amount"] if values["manual_total_amount"] is not None else calculated_total})
        sheets.append({"vehicle_type": rate.vehicle_type, "rate": {
            "weekday_hourly": rate.weekday_hourly, "weekday_daily": rate.weekday_daily,
            "holiday_hourly": rate.holiday_hourly, "holiday_daily": rate.holiday_daily,
            "overtime_hourly": rate.overtime_hourly, "effective_from": rate.effective_from.isoformat(),
        }, "rows": rows})
    return {"site_id": site.id, "site_name": site.name, "year": year, "month": month,
            "tax_included": False, "sheets": sheets}


@router.post("/row")
async def save_row(
    payload: ChargeRow,
    session: Session = Depends(get_session), actor: Employee = Depends(admin),
):
    site, rates = _rates(session, payload.worksite_id)
    if not site.is_active:
        raise HTTPException(409, "此工地已停用，不可新增計價；歷史資料仍可查詢")
    rate = next((item for item in rates if item.vehicle_type == payload.vehicle_type), None)
    if rate is None or payload.work_date < rate.effective_from:
        raise HTTPException(422, "此車型在該日期尚無有效費率；國泰慈文自115年11月開始")
    required = _required_holiday(payload.work_date)
    if required and payload.holiday_reason != required:
        raise HTTPException(422, f"{payload.work_date} 必須採{required}費率")
    if not required and payload.holiday_reason in {"週日", "元旦"}:
        raise HTTPException(422, "此日不是週日或元旦；春節請明確選擇春節")
    if payload.units == 0 and (payload.normal_hours or payload.overtime_hours):
        raise HTTPException(422, "有工時時車輛數量須大於零")
    values = payload.model_dump(exclude={"worksite_id", "work_date", "vehicle_type", "expected_revision",
                                         "request_id", "preview_only", "confirmed"})
    values["external_customer"] = values["external_customer"].strip()
    values["payer"] = values["payer"].strip()
    values["manual_amount_reason"] = values["manual_amount_reason"].strip()
    if (values["manual_total_amount"] is None) != (not values["manual_amount_reason"]):
        raise HTTPException(422, "人工核定金額與調整理由須一起填寫；不調整時兩欄皆留空")
    if values["manual_total_amount"] is not None and payload.units == 0:
        raise HTTPException(422, "沒有車輛不可核定請款金額")
    for key in ("external_customer", "payer", "manual_amount_reason"):
        if values[key].startswith(("=", "+", "-", "@")):
            raise HTTPException(422, f"{key} 不能以公式符號開頭")
    normal_amount, overtime_amount = _amount(rate, payload.units, payload.normal_hours,
                                              payload.overtime_hours, bool(payload.holiday_reason))
    calculated_total = normal_amount + overtime_amount
    total_amount = values["manual_total_amount"] if values["manual_total_amount"] is not None else calculated_total
    current = session.exec(select(WorksiteForkliftCharge).where(
        WorksiteForkliftCharge.worksite_id == payload.worksite_id,
        WorksiteForkliftCharge.work_date == payload.work_date,
        WorksiteForkliftCharge.vehicle_type == payload.vehicle_type,
    )).first()
    if current and current.last_request_id == payload.request_id:
        if _stored_values(current) != values:
            raise HTTPException(409, "同一存檔識別碼的內容不同")
        return {"saved": True, "replayed": True, "revision": current.revision,
                "normal_amount": current.normal_amount, "overtime_amount": current.overtime_amount,
                "calculated_total": current.normal_amount + current.overtime_amount,
                "total_amount": current.manual_total_amount if current.manual_total_amount is not None else current.normal_amount + current.overtime_amount,
                "backup": await _backup()}
    revision = current.revision if current else 0
    if revision != payload.expected_revision:
        raise HTTPException(409, "此日期已由其他人更新，請重新載入核對")
    before = _stored_values(current)
    if payload.preview_only:
        return {"saved": False, "before": before, "after": values, "changed": before != values,
                "normal_amount": normal_amount, "overtime_amount": overtime_amount,
                "calculated_total": calculated_total, "total_amount": total_amount, "tax_included": False}
    if not payload.confirmed:
        raise HTTPException(409, "請先預覽並確認當日金額")
    if before == values:
        return {"saved": True, "unchanged": True, "revision": revision, "backup": await _backup()}
    stored = {**values, "external_customer": values["external_customer"] or None,
              "payer": values["payer"] or None, "manual_amount_reason": values["manual_amount_reason"] or None,
              "normal_amount": normal_amount,
              "overtime_amount": overtime_amount}
    if current:
        result = session.execute(update(WorksiteForkliftCharge).where(
            WorksiteForkliftCharge.id == current.id,
            WorksiteForkliftCharge.revision == revision,
        ).values(**stored, revision=revision + 1, last_request_id=payload.request_id,
                 updated_by_id=actor.id, updated_at=datetime.utcnow()).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            session.rollback()
            raise HTTPException(409, "此日期已由其他人更新，請重新載入")
    else:
        session.add(WorksiteForkliftCharge(
            worksite_id=payload.worksite_id, work_date=payload.work_date, vehicle_type=payload.vehicle_type,
            **stored, last_request_id=payload.request_id, updated_by_id=actor.id,
        ))
    session.add(AdminAuditLog(
        actor_id=actor.id, actor_code=actor.employee_code, actor_name=actor.name,
        action="worksite_pricing_edit", entity_type="worksite", entity_id=payload.worksite_id,
        summary=json.dumps({"day": payload.work_date.isoformat(), "vehicle": payload.vehicle_type,
                            "before": before, "after": values, "normal_amount": normal_amount,
                            "overtime_amount": overtime_amount, "calculated_total": calculated_total,
                            "total_amount": total_amount}, ensure_ascii=False),
    ))
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "此日期已由其他人更新，請重新載入") from exc
    return {"saved": True, "revision": revision + 1, "normal_amount": normal_amount,
            "overtime_amount": overtime_amount, "calculated_total": calculated_total,
            "total_amount": total_amount,
            "tax_included": False, "backup": await _backup()}
