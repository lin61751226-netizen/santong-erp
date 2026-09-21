from datetime import date, datetime, time, timedelta
import unittest

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import (
    AssignmentMember,
    AttendanceEvent,
    Employee,
    Forklift,
    ForkliftInspection,
    LeaveRequest,
    MeetingRecord,
    Role,
    WorkAssignment,
    Worksite,
)


class ManagementExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.admin = Employee(
            employee_code="ADMIN001",
            name="管理者",
            bind_token="ST-ADMIN",
            role=Role.admin,
            must_change_password=False,
        )
        self.employee = Employee(
            employee_code="EMP001",
            name="操作員",
            bind_token="ST-EMP",
            role=Role.employee,
        )
        self.site = Worksite(code="53", name="齊裕53")
        self.session.add_all([self.admin, self.employee, self.site])
        self.session.commit()
        self.session.refresh(self.admin)
        self.session.refresh(self.employee)
        self.session.refresh(self.site)
        app.dependency_overrides[get_session] = lambda: self.session
        app.dependency_overrides[get_current_actor] = lambda: self.admin
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.session.close()
        self.engine.dispose()

    def test_contract_and_certificate_records_are_saved_and_filterable(self) -> None:
        contract = self.client.post(
            "/api/contracts",
            json={
                "title": "齊裕53 堆高機合約",
                "contract_type": "客戶合約",
                "party_name": "齊裕營造",
                "employee_code": "EMP001",
                "site_id": self.site.id,
                "expiry_date": (date.today() + timedelta(days=10)).isoformat(),
                "drive_url": "https://drive.google.com/contract",
            },
        )
        self.assertEqual(contract.status_code, 201)
        self.assertEqual(contract.json()["contract"]["status"]["code"], "expiring")

        certificate = self.client.post(
            "/api/certificates",
            json={
                "employee_code": "EMP001",
                "name": "堆高機操作證",
                "certificate_no": "CERT-001",
                "expiry_date": (date.today() + timedelta(days=400)).isoformat(),
            },
        )
        self.assertEqual(certificate.status_code, 201)

        contracts = self.client.get("/api/contracts?keyword=齊裕53")
        certificates = self.client.get("/api/certificates?employee_code=EMP001")
        self.assertEqual(contracts.status_code, 200)
        self.assertEqual(certificates.status_code, 200)
        self.assertEqual(len(contracts.json()), 1)
        self.assertEqual(certificates.json()[0]["certificate_no"], "CERT-001")

    def test_calendar_combines_assignments_leave_meeting_and_inspection(self) -> None:
        target = date.today()
        assignment = WorkAssignment(
            work_date=target,
            site_id=self.site.id,
            work_item="物料搬運",
            start_time=time(8),
            end_time=time(17),
        )
        self.session.add(assignment)
        self.session.commit()
        self.session.refresh(assignment)
        self.session.add(AssignmentMember(assignment_id=assignment.id, employee_id=self.employee.id))
        self.session.add(LeaveRequest(
            employee_id=self.employee.id,
            leave_type="事假",
            start_date=target,
            end_date=target,
            reason="家庭因素",
        ))
        self.session.add(MeetingRecord(
            title="現場會議",
            meeting_at=datetime.combine(target, time(10)),
            attendee_codes=[self.employee.employee_code],
            agenda="進度",
            decisions="確認",
        ))
        forklift = Forklift(forklift_code="1號", model="2.5T", current_site_id=self.site.id)
        self.session.add(forklift)
        self.session.commit()
        self.session.refresh(forklift)
        self.session.add(ForkliftInspection(
            forklift_id=forklift.id,
            operator_id=self.employee.id,
            site_id=self.site.id,
            inspection_date=target,
            all_passed=True,
        ))
        self.session.commit()

        response = self.client.get(f"/api/calendar?start_date={target}&end_date={target}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"], {
            "assignment": 1,
            "leave": 1,
            "meeting": 1,
            "inspection": 1,
        })

    def test_forklift_attendance_stats_merges_location_events(self) -> None:
        target = date.today()
        forklift = Forklift(forklift_code="1號", model="2.5T", current_site_id=self.site.id)
        self.session.add(forklift)
        self.session.commit()
        self.session.refresh(forklift)
        self.session.add(ForkliftInspection(
            forklift_id=forklift.id,
            operator_id=self.employee.id,
            site_id=self.site.id,
            inspection_date=target,
            all_passed=True,
        ))
        self.session.add(AttendanceEvent(
            employee_id=self.employee.id,
            site_id=self.site.id,
            event_type="上班打卡",
            happened_at=datetime.combine(target, time(8)),
        ))
        self.session.add(AttendanceEvent(
            employee_id=self.employee.id,
            site_id=self.site.id,
            event_type="下班打卡",
            happened_at=datetime.combine(target, time(17)),
        ))
        self.session.commit()

        response = self.client.get(
            f"/api/forklift-attendance/stats?date_from={target}&date_to={target}"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["records"], 1)
        self.assertEqual(data["summary"]["total_hours"], 9.0)
        self.assertEqual(data["rows"][0]["forklift_code"], "1號")
        self.assertEqual(data["rows"][0]["check_in"], datetime.combine(target, time(8)).isoformat())


if __name__ == "__main__":
    unittest.main()
