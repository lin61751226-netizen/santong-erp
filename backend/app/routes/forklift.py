"""堆高機出租管理 API：客戶、設備、出租記錄的 CRUD 與儀表板統計。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.models import (
    Employee,
    ForkliftCustomer,
    ForkliftEquipment,
    ForkliftRental,
)
from app.schemas import (
    ForkliftCustomerCreate,
    ForkliftCustomerUpdate,
    ForkliftEquipmentCreate,
    ForkliftEquipmentUpdate,
    ForkliftRentalCreate,
    ForkliftRentalUpdate,
)

router = APIRouter(prefix="/api/forklift", tags=["forklift"])


def _next_code(session: Session, model, code_field: str, prefix: str) -> str:
    """產生下一個編號，例如 C001、F001、R001。"""
    rows = session.exec(select(model)).all()
    max_num = 0
    for row in rows:
        code = getattr(row, code_field)
        if code and code.startswith(prefix):
            try:
                num = int(code[len(prefix):])
                max_num = max(max_num, num)
            except ValueError:
                pass
    return f"{prefix}{str(max_num + 1).zfill(3)}"


# ---- 客戶管理 ----

@router.get("/customers")
def list_customers(
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    rows = session.exec(select(ForkliftCustomer).order_by(ForkliftCustomer.customer_code)).all()
    return [
        {
            "id": row.id,
            "customer_code": row.customer_code,
            "name": row.name,
            "tax_id": row.tax_id,
            "contact": row.contact,
            "phone": row.phone,
            "email": row.email,
            "credit_limit": row.credit_limit,
            "payment_terms": row.payment_terms,
            "grade": row.grade,
        }
        for row in rows
    ]


@router.post("/customers")
def create_customer(
    payload: ForkliftCustomerCreate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    customer = ForkliftCustomer(
        customer_code=_next_code(session, ForkliftCustomer, "customer_code", "C"),
        name=payload.name,
        tax_id=payload.tax_id,
        contact=payload.contact,
        phone=payload.phone,
        email=payload.email,
        credit_limit=payload.credit_limit,
        payment_terms=payload.payment_terms,
        grade=payload.grade,
    )
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return customer


@router.put("/customers/{customer_id}")
def update_customer(
    customer_id: int,
    payload: ForkliftCustomerUpdate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    customer = session.get(ForkliftCustomer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="找不到客戶")
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(customer, key, value)
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return customer


@router.delete("/customers/{customer_id}")
def delete_customer(
    customer_id: int,
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    customer = session.get(ForkliftCustomer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="找不到客戶")
    # 檢查是否有關聯出租記錄
    linked = session.exec(select(ForkliftRental).where(ForkliftRental.customer_id == customer_id)).first()
    if linked is not None:
        raise HTTPException(status_code=400, detail="此客戶有出租記錄，無法刪除")
    session.delete(customer)
    session.commit()
    return {"message": "客戶已刪除"}


# ---- 設備管理 ----

@router.get("/equipment")
def list_equipment(
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    rows = session.exec(select(ForkliftEquipment).order_by(ForkliftEquipment.equipment_code)).all()
    return [
        {
            "id": row.id,
            "equipment_code": row.equipment_code,
            "name": row.name,
            "brand": row.brand,
            "capacity": row.capacity,
            "fuel_type": row.fuel_type,
            "status": row.status.value if hasattr(row.status, "value") else row.status,
            "daily_rate": row.daily_rate,
            "monthly_rate": row.monthly_rate,
        }
        for row in rows
    ]


@router.post("/equipment")
def create_equipment(
    payload: ForkliftEquipmentCreate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    equipment = ForkliftEquipment(
        equipment_code=_next_code(session, ForkliftEquipment, "equipment_code", "F"),
        name=payload.name,
        brand=payload.brand,
        capacity=payload.capacity,
        fuel_type=payload.fuel_type,
        status=payload.status,
        daily_rate=payload.daily_rate,
        monthly_rate=payload.monthly_rate,
    )
    session.add(equipment)
    session.commit()
    session.refresh(equipment)
    return equipment


@router.put("/equipment/{equipment_id}")
def update_equipment(
    equipment_id: int,
    payload: ForkliftEquipmentUpdate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    equipment = session.get(ForkliftEquipment, equipment_id)
    if equipment is None:
        raise HTTPException(status_code=404, detail="找不到設備")
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(equipment, key, value)
    session.add(equipment)
    session.commit()
    session.refresh(equipment)
    return equipment


@router.delete("/equipment/{equipment_id}")
def delete_equipment(
    equipment_id: int,
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    equipment = session.get(ForkliftEquipment, equipment_id)
    if equipment is None:
        raise HTTPException(status_code=404, detail="找不到設備")
    linked = session.exec(select(ForkliftRental).where(ForkliftRental.equipment_id == equipment_id)).first()
    if linked is not None:
        raise HTTPException(status_code=400, detail="此設備有出租記錄，無法刪除")
    session.delete(equipment)
    session.commit()
    return {"message": "設備已刪除"}


# ---- 出租管理 ----

@router.get("/rentals")
def list_rentals(
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    rows = session.exec(select(ForkliftRental).order_by(ForkliftRental.rental_code)).all()
    result = []
    for row in rows:
        customer = session.get(ForkliftCustomer, row.customer_id)
        equipment = session.get(ForkliftEquipment, row.equipment_id)
        result.append({
            "id": row.id,
            "rental_code": row.rental_code,
            "customer_id": row.customer_id,
            "customer_name": customer.name if customer else "未知客戶",
            "equipment_id": row.equipment_id,
            "equipment_name": equipment.name if equipment else "未知設備",
            "start_date": row.start_date.isoformat(),
            "end_date": row.end_date.isoformat(),
            "days": row.days,
            "daily_rate": row.daily_rate,
            "total_amount": row.total_amount,
            "deposit": row.deposit,
            "tax": row.tax,
            "status": row.status.value if hasattr(row.status, "value") else row.status,
        })
    return result


@router.post("/rentals")
def create_rental(
    payload: ForkliftRentalCreate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    customer = session.get(ForkliftCustomer, payload.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="找不到客戶")
    equipment = session.get(ForkliftEquipment, payload.equipment_id)
    if equipment is None:
        raise HTTPException(status_code=404, detail="找不到設備")

    rental = ForkliftRental(
        rental_code=_next_code(session, ForkliftRental, "rental_code", "R"),
        customer_id=payload.customer_id,
        equipment_id=payload.equipment_id,
        start_date=date.fromisoformat(payload.start_date),
        end_date=date.fromisoformat(payload.end_date),
        days=payload.days,
        daily_rate=payload.daily_rate,
        total_amount=payload.total_amount,
        deposit=payload.deposit,
        tax=payload.tax,
        status=payload.status,
    )
    session.add(rental)
    # 新增出租記錄時，若狀態為進行中，將設備標記為已租
    if payload.status == "進行中":
        equipment.status = "已租"
        session.add(equipment)
    session.commit()
    session.refresh(rental)
    return rental


@router.put("/rentals/{rental_id}")
def update_rental(
    rental_id: int,
    payload: ForkliftRentalUpdate,
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    rental = session.get(ForkliftRental, rental_id)
    if rental is None:
        raise HTTPException(status_code=404, detail="找不到出租記錄")
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        if key in ("start_date", "end_date") and value is not None:
            value = date.fromisoformat(value)
        setattr(rental, key, value)
    session.add(rental)
    session.commit()
    session.refresh(rental)
    return rental


@router.delete("/rentals/{rental_id}")
def delete_rental(
    rental_id: int,
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    rental = session.get(ForkliftRental, rental_id)
    if rental is None:
        raise HTTPException(status_code=404, detail="找不到出租記錄")
    # 刪除出租記錄時，將關聯設備標記為可租
    equipment = session.get(ForkliftEquipment, rental.equipment_id)
    if equipment is not None and equipment.status == "已租":
        equipment.status = "可租"
        session.add(equipment)
    session.delete(rental)
    session.commit()
    return {"message": "出租記錄已刪除"}


# ---- 儀表板統計 ----

@router.get("/stats")
def get_stats(
    session: Session = Depends(get_session),
    actor: Employee = Depends(get_current_actor),
):
    rentals = session.exec(select(ForkliftRental)).all()
    equipment = session.exec(select(ForkliftEquipment)).all()

    total_income = sum(r.total_amount for r in rentals)
    total_deposit = sum(r.deposit for r in rentals)
    total_tax = sum(r.tax for r in rentals)
    receivables = sum(r.total_amount for r in rentals if r.status == "進行中")

    rented_count = sum(1 for e in equipment if e.status == "已租")
    total_equipment = len(equipment)
    utilization = round((rented_count / total_equipment * 100), 1) if total_equipment > 0 else 0

    # 設備狀態分布
    status_dist = {}
    for e in equipment:
        status_val = e.status.value if hasattr(e.status, "value") else e.status
        status_dist[status_val] = status_dist.get(status_val, 0) + 1

    return {
        "total_income": total_income,
        "total_deposit": total_deposit,
        "total_tax": total_tax,
        "receivables": receivables,
        "equipment_utilization": utilization,
        "equipment_status": status_dist,
        "rental_count": len(rentals),
        "customer_count": len(session.exec(select(ForkliftCustomer)).all()),
        "equipment_count": total_equipment,
    }
