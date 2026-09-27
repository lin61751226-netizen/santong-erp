from datetime import date, datetime, time, timedelta
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import (
    AssignmentMember,
    AttendanceEvent,
    CertificateRecord,
    ContractRecord,
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

    def test_contract_and_certificate_file_upload_save_private_drive_links(self) -> None:
        drive_file = Mock(file_url="https://drive.google.com/file/d/private-file/view")
        with (
            patch("app.routes.admin.google_drive_worklog_service.upload_business_attachment", new_callable=AsyncMock, return_value=drive_file) as upload,
            patch("app.routes.admin.google_drive_worklog_service.backup_database", new_callable=AsyncMock, return_value={"status": "uploaded"}) as backup,
        ):
            contract = self.client.post(
                "/api/contracts/with-file",
                data={"payload": json.dumps({"title": "齊裕53 合約", "site_id": self.site.id})},
                files={"file": ("contract.pdf", b"%PDF-1.4\nexample", "application/pdf")},
            )
            certificate = self.client.post(
                "/api/certificates/with-file",
                data={"payload": json.dumps({"employee_code": "EMP001", "name": "操作證"})},
                files={"file": ("certificate.jpg", b"\xff\xd8\xffexample", "image/jpeg")},
            )

        self.assertEqual(contract.status_code, 201, contract.text)
        self.assertEqual(certificate.status_code, 201, certificate.text)
        self.assertEqual(contract.json()["contract"]["drive_url"], drive_file.file_url)
        self.assertEqual(certificate.json()["certificate"]["drive_url"], drive_file.file_url)
        self.assertEqual(contract.json()["backup_status"], "uploaded")
        self.assertEqual(upload.await_args_list[0].kwargs["kind"], "contract")
        self.assertEqual(upload.await_args_list[1].kwargs["kind"], "certificate")
        self.assertEqual(backup.await_count, 2)

    def test_invalid_file_or_record_never_uploads(self) -> None:
        with patch("app.routes.admin.google_drive_worklog_service.upload_business_attachment", new_callable=AsyncMock) as upload:
            invalid_date = self.client.post(
                "/api/contracts/with-file",
                data={"payload": json.dumps({"title": "錯誤合約", "start_date": "2026-10-02", "expiry_date": "2026-10-01"})},
                files={"file": ("contract.pdf", b"%PDF-1.4\nexample", "application/pdf")},
            )
            invalid_file = self.client.post(
                "/api/certificates/with-file",
                data={"payload": json.dumps({"employee_code": "EMP001", "name": "操作證"})},
                files={"file": ("certificate.pdf", b"not a PDF", "application/pdf")},
            )
            competing_link = self.client.post(
                "/api/contracts/with-file",
                data={"payload": json.dumps({"title": "重複來源", "drive_url": "https://drive.google.com/file"})},
                files={"file": ("contract.pdf", b"%PDF-1.4\nexample", "application/pdf")},
            )
        self.assertEqual(invalid_date.status_code, 400)
        self.assertEqual(invalid_file.status_code, 400)
        self.assertEqual(competing_link.status_code, 400)
        upload.assert_not_awaited()
        self.assertEqual(len(self.session.exec(select(ContractRecord)).all()), 0)
        self.assertEqual(len(self.session.exec(select(CertificateRecord)).all()), 0)

    def test_employee_cannot_upload_business_attachment(self) -> None:
        app.dependency_overrides[get_current_actor] = lambda: self.employee
        with patch("app.routes.admin.google_drive_worklog_service.upload_business_attachment", new_callable=AsyncMock) as upload:
            response = self.client.post(
                "/api/certificates/with-file",
                data={"payload": json.dumps({"employee_code": "EMP001", "name": "操作證"})},
                files={"file": ("certificate.pdf", b"%PDF-1.4\nexample", "application/pdf")},
            )
        self.assertEqual(response.status_code, 403)
        upload.assert_not_awaited()

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
