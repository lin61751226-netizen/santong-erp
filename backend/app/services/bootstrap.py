from __future__ import annotations

from sqlmodel import Session, select

from app.models import Employee, EmployeeStatus, Role, Worksite


WORKSITE_NAMES = [
    "45",
    "53",
    "56",
    "善捷47",
    "金駿76",
    "桃園28",
    "桃園29",
    "新竹寶山1",
    "新竹寶山2",
    "新竹寶山3",
]


EMPLOYEE_ROSTER = [
    {
        "employee_code": "BOSS001",
        "name": "三通工程行林老闆",
        "bind_token": "ST-1001",
        "role": Role.owner,
        "title": "老闆",
        "department": "經營管理",
        "salary_scheme": "月薪",
        "status": EmployeeStatus.active,
    },
    {
        "employee_code": "ADMIN001",
        "name": "林金谷",
        "bind_token": "ST-1002",
        "role": Role.admin,
        "title": "系統管理者",
        "department": "系統管理",
        "salary_scheme": "月薪",
        "status": EmployeeStatus.active,
    },
    {
        "employee_code": "BOT001",
        "name": "三通工程行,line機器人",
        "bind_token": "ST-1003",
        "role": Role.external,
        "title": "LINE機器人",
        "department": "系統管理",
        "salary_scheme": "系統帳號",
        "status": EmployeeStatus.active,
    },
    {
        "employee_code": "ADMIN002",
        "name": "秀蓉ε٩(๑> ₃ <)7з",
        "bind_token": "ST-1004",
        "role": Role.admin,
        "title": "行政人員",
        "department": "行政",
        "salary_scheme": "月薪",
        "status": EmployeeStatus.active,
    },
    {
        "employee_code": "EMP001",
        "name": "勝忠",
        "bind_token": "ST-1005",
        "role": Role.employee,
        "title": "現場人員",
        "department": "工程",
        "salary_scheme": "日薪",
        "status": EmployeeStatus.active,
    },
    {
        "employee_code": "EMP002",
        "name": "小咪",
        "bind_token": "ST-1006",
        "role": Role.employee,
        "title": "現場人員",
        "department": "工程",
        "salary_scheme": "日薪",
        "status": EmployeeStatus.active,
    },
    {
        "employee_code": "EMP003",
        "name": "達成Ray Rostova",
        "bind_token": "ST-1007",
        "role": Role.employee,
        "title": "現場人員",
        "department": "工程",
        "salary_scheme": "日薪",
        "status": EmployeeStatus.active,
    },
    {
        "employee_code": "EMP004",
        "name": "林小咪",
        "bind_token": "ST-1008",
        "role": Role.employee,
        "title": "現場人員",
        "department": "工程",
        "salary_scheme": "日薪",
        "status": EmployeeStatus.active,
    },
]


LEGACY_EMPLOYEE_CODE_MAP = {
    "ACC001": "BOT001",
    "SUP047": "ADMIN002",
}


def _ensure_worksites(session: Session) -> dict[str, Worksite]:
    existing_sites = session.exec(select(Worksite).order_by(Worksite.id)).all()
    site_by_code = {site.code: site for site in existing_sites}
    site_by_name = {site.name: site for site in existing_sites}

    changed = False
    for worksite_name in WORKSITE_NAMES:
        site = site_by_code.get(worksite_name) or site_by_name.get(worksite_name)
        if site is None:
            site = Worksite(code=worksite_name, name=worksite_name, is_active=True)
            session.add(site)
            changed = True
            continue

        if site.code != worksite_name:
            site.code = worksite_name
            changed = True
        if site.name != worksite_name:
            site.name = worksite_name
            changed = True
        if not site.is_active:
            site.is_active = True
            changed = True
        session.add(site)

    if changed:
        session.commit()

    return {site.name: site for site in session.exec(select(Worksite)).all()}


def _rename_legacy_employee_codes(session: Session) -> None:
    changed = False
    for old_code, new_code in LEGACY_EMPLOYEE_CODE_MAP.items():
        employee = session.exec(select(Employee).where(Employee.employee_code == old_code)).first()
        if not employee:
            continue

        collision = session.exec(select(Employee).where(Employee.employee_code == new_code)).first()
        if collision and collision.id != employee.id:
            continue

        employee.employee_code = new_code
        session.add(employee)
        changed = True

    if changed:
        session.commit()


def _upsert_employee(session: Session, payload: dict) -> None:
    employee = session.exec(select(Employee).where(Employee.employee_code == payload["employee_code"])).first()
    if employee is None:
        employee = Employee(
            employee_code=payload["employee_code"],
            bind_token=payload["bind_token"],
            name=payload["name"],
        )

    preserved_line_user_id = employee.line_user_id

    employee.name = payload["name"]
    employee.bind_token = payload["bind_token"]
    employee.role = payload["role"]
    employee.title = payload["title"]
    employee.department = payload["department"]
    employee.salary_scheme = payload["salary_scheme"]
    employee.status = payload["status"]
    employee.line_user_id = preserved_line_user_id
    employee.home_site_id = None
    employee.phone = employee.phone or None
    employee.hire_date = employee.hire_date or None
    employee.labor_insurance_note = employee.labor_insurance_note or None
    employee.emergency_contact = employee.emergency_contact or None
    employee.contract_expiry = employee.contract_expiry or None
    employee.licenses = list(employee.licenses or [])
    employee.training_records = list(employee.training_records or [])
    employee.machine_skills = list(employee.machine_skills or [])

    session.add(employee)


def _deactivate_unlisted_employees(session: Session) -> None:
    roster_codes = {item["employee_code"] for item in EMPLOYEE_ROSTER}
    employees = session.exec(select(Employee)).all()
    changed = False
    for employee in employees:
        if employee.employee_code in roster_codes:
            continue
        if employee.status != EmployeeStatus.inactive:
            employee.status = EmployeeStatus.inactive
            session.add(employee)
            changed = True
    if changed:
        session.commit()


def seed_demo_data(session: Session) -> None:
    _ensure_worksites(session)
    _rename_legacy_employee_codes(session)

    for payload in EMPLOYEE_ROSTER:
        _upsert_employee(session, payload)
    session.commit()

    _deactivate_unlisted_employees(session)
