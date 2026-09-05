from __future__ import annotations

from sqlmodel import Session, select

from app.core.config import settings
from app.core.security import hash_password
from app.models import Employee, EmployeeStatus, Forklift, ForkliftStatus, Role, Worksite


WORKSITE_DEFINITIONS = [
    {"code": "45", "name": "45"},
    {"code": "53", "name": "齊裕53"},
    {"code": "56", "name": "56"},
    {"code": "善捷47", "name": "善捷47"},
    {"code": "金駿76", "name": "金駿76"},
    {"code": "桃園28", "name": "桃園28"},
    {"code": "桃園29", "name": "桃園29"},
    {"code": "新竹寶山1", "name": "新竹寶山1"},
    {"code": "新竹寶山2", "name": "新竹寶山2"},
    {"code": "新竹寶山3", "name": "新竹寶山3"},
]


SITE_ALIASES = {
    "45": "45",
    "53": "齊裕53",
    "56": "56",
    "47": "善捷47",
    "善捷47": "善捷47",
    "金駿76": "金駿76",
    "桃園28": "桃園28",
    "桃園29": "桃園29",
    "齊裕53": "齊裕53",
    "新竹寶山1": "新竹寶山1",
    "新竹寶山2": "新竹寶山2",
    "新竹寶山3": "新竹寶山3",
}


EMPLOYEE_ROSTER = [
    {
        "employee_code": "BOSS001",
        "name": "三通工程行林老闆",
        "bind_token": "ST-1001",
        "role": Role.owner,
        "title": "老闆",
        "department": "經營管理",
        "salary_scheme": "月薪",
        "phone": "0978909078",
        "email": "a0900262580@gmail.com",
        "assigned_sites": ["新竹寶山1", "新竹寶山2", "新竹寶山3"],
        "machine_skills": [],
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
        "phone": "0937069276",
        "email": "lin61751226@gmail.com",
        "assigned_sites": [],
        "machine_skills": [],
        "status": EmployeeStatus.active,
    },
    {
        "employee_code": "BOT001",
        "name": "三通工程行line機器人",
        "bind_token": "ST-1009",
        "role": Role.external,
        "title": "系統管理者",
        "department": "系統管理",
        "salary_scheme": "系統帳號",
        "phone": None,
        "email": None,
        "assigned_sites": [],
        "machine_skills": [],
        "status": EmployeeStatus.active,
    },
    {
        "employee_code": "ADMIN002",
        "name": "秀蓉",
        "bind_token": "ST-1003",
        "role": Role.admin,
        "title": "行政人員",
        "department": "行政",
        "salary_scheme": "月薪",
        "phone": "0921375095",
        "email": "linxiaoniu4@gmail.com",
        "assigned_sites": [],
        "machine_skills": [],
        "status": EmployeeStatus.active,
    },
    {
        "employee_code": "EMP001",
        "name": "勝忠",
        "bind_token": "ST-1005",
        "role": Role.employee,
        "title": "堆高機司機",
        "department": "工程",
        "salary_scheme": "日薪",
        "phone": "0000000053",
        "email": "zhushengzhong12@gmail.com",
        "assigned_sites": ["齊裕53"],
        "machine_skills": ["堆高機"],
        "status": EmployeeStatus.active,
    },
    {
        "employee_code": "EMP003",
        "name": "建成Ray Rostova",
        "bind_token": "ST-1007",
        "role": Role.employee,
        "title": "堆高機司機",
        "department": "工程",
        "salary_scheme": "日薪",
        "phone": "0000000053",
        "email": "rayrostova@gmail.com",
        "assigned_sites": ["齊裕53"],
        "machine_skills": ["堆高機"],
        "status": EmployeeStatus.active,
    },
    {
        "employee_code": "EMP005",
        "name": "林小咪",
        "bind_token": "ST-1008",
        "role": Role.employee,
        "title": "堆高機司機",
        "department": "工程",
        "salary_scheme": "日薪",
        "phone": "0937028972",
        "email": "lin275400@gmail.com",
        "assigned_sites": ["善捷47", "金駿76", "桃園28", "桃園29"],
        "machine_skills": ["堆高機"],
        "status": EmployeeStatus.active,
    },
]


LEGACY_EMPLOYEE_CODE_MAP = {
    "ACC001": "BOT001",
    "SUP047": "ADMIN002",
}

FORKLIFT_DEFINITIONS = [
    {"code": "1號", "model": "自排 2.5噸柴油車", "site_name": "永森45", "fuel_level": 68},
    {"code": "2號", "model": "手排 2.5噸柴油車", "site_name": "齊裕53", "fuel_level": 92},
    {"code": "3號", "model": "自排 3.0噸柴油車", "site_name": "金駿76", "fuel_level": 74},
    {"code": "5號", "model": "手排 3.0噸柴油車", "site_name": "桃園28", "fuel_level": 54},
    {"code": "6號", "model": "自排 2.5噸柴油車", "site_name": "桃園29", "fuel_level": 80},
    {"code": "7號", "model": "手排 3.0噸柴油車", "site_name": "新竹寶山1", "fuel_level": 85},
]



def _normalized_sites(site_names: list[str]) -> list[str]:
    normalized: list[str] = []
    for item in site_names:
        canonical = SITE_ALIASES.get(item.strip(), item.strip())
        if canonical and canonical not in normalized:
            normalized.append(canonical)
    return normalized


def _ensure_worksites(session: Session) -> dict[str, Worksite]:
    existing_sites = session.exec(select(Worksite).order_by(Worksite.id)).all()
    site_by_code = {site.code: site for site in existing_sites}
    site_by_name = {site.name: site for site in existing_sites}

    changed = False
    for definition in WORKSITE_DEFINITIONS:
        code = definition["code"]
        name = definition["name"]
        site = site_by_code.get(code) or site_by_name.get(name)
        if site is None:
            site = Worksite(code=code, name=name, is_active=True)
            session.add(site)
            changed = True
            continue

        if site.code != code:
            site.code = code
            changed = True
        if site.name != name:
            site.name = name
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


def _upsert_employee(session: Session, payload: dict, worksites: dict[str, Worksite]) -> None:
    employee = session.exec(select(Employee).where(Employee.employee_code == payload["employee_code"])).first()
    if employee is None:
        employee = Employee(
            employee_code=payload["employee_code"],
            bind_token=payload["bind_token"],
            name=payload["name"],
        )

    normalized_sites = _normalized_sites(payload.get("assigned_sites", []))
    primary_site_name = normalized_sites[0] if normalized_sites else None
    primary_site = worksites.get(primary_site_name) if primary_site_name else None
    preserved_line_user_id = employee.line_user_id

    employee.name = payload["name"]
    employee.bind_token = payload["bind_token"]
    employee.role = payload["role"]
    employee.title = payload["title"]
    employee.department = payload["department"]
    employee.salary_scheme = payload["salary_scheme"]
    employee.status = payload["status"]
    employee.line_user_id = preserved_line_user_id
    employee.home_site_id = primary_site.id if primary_site else None
    employee.phone = payload.get("phone")
    employee.email = payload.get("email")
    employee.hire_date = employee.hire_date or None
    employee.labor_insurance_note = employee.labor_insurance_note or None
    employee.emergency_contact = employee.emergency_contact or None
    employee.contract_expiry = employee.contract_expiry or None
    employee.licenses = list(employee.licenses or [])
    employee.training_records = list(employee.training_records or [])
    employee.machine_skills = list(payload.get("machine_skills", employee.machine_skills or []))
    employee.assigned_sites = normalized_sites

    # 後台登入帳號（owner/admin）若尚未設定密碼，以統一預設密碼初始化並強制改密碼；
    # 已設定過密碼（已改過密碼）的帳號一律不覆蓋，確保使用者改過的密碼不會被 seed 重置。
    if employee.role in {Role.owner, Role.admin} and not employee.password_hash:
        employee.password_hash = hash_password(settings.default_password)
        employee.must_change_password = True
        employee.failed_login_count = 0
        employee.locked_until = None

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




def _ensure_forklifts(session: Session, worksites: dict[str, Worksite]) -> None:
    existing = {f.forklift_code: f for f in session.exec(select(Forklift)).all()}
    changed = False
    for definition in FORKLIFT_DEFINITIONS:
        code = definition["code"]
        forklift = existing.get(code)
        site = worksites.get(definition["site_name"])
        if forklift is None:
            forklift = Forklift(
                forklift_code=code,
                model=definition["model"],
                status=ForkliftStatus.available,
                current_site_id=site.id if site else None,
                fuel_level=definition["fuel_level"],
            )
            session.add(forklift)
            changed = True
        else:
            if forklift.model != definition["model"]:
                forklift.model = definition["model"]
                changed = True
            if site and forklift.current_site_id != site.id:
                forklift.current_site_id = site.id
                changed = True
            if forklift.fuel_level is None:
                forklift.fuel_level = definition["fuel_level"]
                changed = True
            if forklift.status == ForkliftStatus.inactive:
                forklift.status = ForkliftStatus.available
                changed = True
            session.add(forklift)
    if changed:
        session.commit()


def seed_demo_data(session: Session) -> None:
    worksites = _ensure_worksites(session)
    _rename_legacy_employee_codes(session)

    for payload in EMPLOYEE_ROSTER:
        _upsert_employee(session, payload, worksites)
    session.commit()

    _deactivate_unlisted_employees(session)
    _ensure_forklifts(session, worksites)

    # 自動解鎖管理員帳號（owner/admin），防止被永久鎖定導致無法登入後台
    for emp in session.exec(
        select(Employee).where(Employee.role.in_([Role.owner, Role.admin]))
    ).all():
        if emp.locked_until or emp.failed_login_count:
            emp.locked_until = None
            emp.failed_login_count = 0
            session.add(emp)
    session.commit()

