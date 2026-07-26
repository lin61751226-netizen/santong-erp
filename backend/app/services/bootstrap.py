from datetime import date, time, timedelta

from sqlmodel import Session, select

from app.models import (
    AckStatus,
    AssignmentMember,
    AttendanceEvent,
    AttendanceEventType,
    Employee,
    EmployeeStatus,
    LeaveRequest,
    LeaveStatus,
    Role,
    WorkAssignment,
    Worksite,
)


def seed_demo_data(session: Session) -> None:
    has_employee = session.exec(select(Employee.id)).first()
    if has_employee:
        return

    site_names = [
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

    for name in site_names:
        worksite = Worksite(code=name, name=name)
        session.add(worksite)
    session.commit()

    site_map = {site.name: site for site in session.exec(select(Worksite)).all()}

    employees = [
        Employee(
            employee_code="BOSS001",
            name="三通老闆",
            bind_token="ST-1001",
            role=Role.owner,
            title="老闆",
            department="管理部",
            salary_scheme="月薪",
            emergency_contact="總機 03-1234567",
            status=EmployeeStatus.active,
        ),
        Employee(
            employee_code="ADMIN001",
            name="行政主管",
            bind_token="ST-1002",
            role=Role.admin,
            title="行政主管",
            department="管理部",
            salary_scheme="月薪",
            emergency_contact="總機 03-1234567",
            status=EmployeeStatus.active,
        ),
        Employee(
            employee_code="ACC001",
            name="會計小姐",
            bind_token="ST-1003",
            role=Role.accounting,
            title="會計",
            department="財務部",
            salary_scheme="月薪",
            status=EmployeeStatus.active,
        ),
        Employee(
            employee_code="SUP047",
            name="林主任",
            bind_token="ST-1004",
            role=Role.site_manager,
            title="工地主任",
            department="工務部",
            home_site_id=site_map["善捷47"].id,
            salary_scheme="月薪",
            machine_skills=["堆高機", "現場調度"],
            status=EmployeeStatus.active,
        ),
        Employee(
            employee_code="EMP001",
            name="王小明",
            bind_token="ST-1005",
            role=Role.employee,
            title="現場人員",
            department="工務部",
            home_site_id=site_map["善捷47"].id,
            salary_scheme="日薪",
            machine_skills=["堆高機"],
            status=EmployeeStatus.active,
        ),
        Employee(
            employee_code="EMP002",
            name="李小華",
            bind_token="ST-1006",
            role=Role.employee,
            title="現場人員",
            department="工務部",
            home_site_id=site_map["善捷47"].id,
            salary_scheme="日薪",
            machine_skills=["物料整理"],
            status=EmployeeStatus.active,
        ),
        Employee(
            employee_code="EMP003",
            name="陳志宏",
            bind_token="ST-1007",
            role=Role.employee,
            title="機具操作員",
            department="工務部",
            home_site_id=site_map["桃園29"].id,
            salary_scheme="日薪",
            machine_skills=["堆高機", "吊掛"],
            status=EmployeeStatus.active,
        ),
    ]

    for employee in employees:
        session.add(employee)
    session.commit()

    employee_map = {item.employee_code: item for item in session.exec(select(Employee)).all()}
    tomorrow = date.today() + timedelta(days=1)

    assignment = WorkAssignment(
        work_date=tomorrow,
        site_id=site_map["善捷47"].id,
        work_item="堆高機移料、現場物料整理",
        supervisor_id=employee_map["SUP047"].id,
        start_time=time(hour=7, minute=40),
        end_time=time(hour=17, minute=0),
        vehicle="3.5T 貨車",
        equipment="堆高機",
        notes="進場前完成安全檢查",
        created_by=employee_map["ADMIN001"].id,
    )
    session.add(assignment)
    session.commit()
    session.refresh(assignment)

    for code in ["EMP001", "EMP002"]:
        member = AssignmentMember(
            assignment_id=assignment.id,
            employee_id=employee_map[code].id,
            ack_status=AckStatus.pending,
        )
        session.add(member)

    sunday_assignment = WorkAssignment(
        work_date=date.today(),
        site_id=site_map["善捷47"].id,
        work_item="週日臨時移料與安全巡檢",
        supervisor_id=employee_map["SUP047"].id,
        start_time=time(hour=7, minute=30),
        end_time=time(hour=12, minute=0),
        vehicle="3.5T 貨車",
        equipment="堆高機",
        notes="週日出勤需另計加班或換休",
        created_by=employee_map["ADMIN001"].id,
    )
    session.add(sunday_assignment)
    session.commit()
    session.refresh(sunday_assignment)

    session.add(
        AssignmentMember(
            assignment_id=sunday_assignment.id,
            employee_id=employee_map["EMP001"].id,
            ack_status=AckStatus.arrived,
            last_line_action="到達工地",
        )
    )
    session.add(
        AttendanceEvent(
            employee_id=employee_map["EMP001"].id,
            site_id=site_map["善捷47"].id,
            assignment_id=sunday_assignment.id,
            event_type=AttendanceEventType.arrive_site.value,
        )
    )
    session.add(
        LeaveRequest(
            employee_id=employee_map["EMP003"].id,
            leave_type="排休",
            start_date=tomorrow,
            end_date=tomorrow,
            reason="月度排休申請",
            status=LeaveStatus.pending,
            policy_note="已超過月初統一排假建議時間，請主管確認。",
        )
    )
    session.commit()
