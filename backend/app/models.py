from __future__ import annotations

from datetime import date, datetime, time
from enum import Enum
from typing import Optional

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel


class Role(str, Enum):
    owner = "owner"
    admin = "admin"
    accounting = "accounting"
    site_manager = "site_manager"
    employee = "employee"
    external = "external"


class EmployeeStatus(str, Enum):
    active = "active"
    inactive = "inactive"


class AssignmentStatus(str, Enum):
    scheduled = "scheduled"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"


class AckStatus(str, Enum):
    pending = "pending"
    received = "received"
    arrived = "arrived"
    started = "started"
    completed = "completed"
    exception = "exception"


class NotificationCategory(str, Enum):
    ad_hoc = "ad_hoc"
    daily_schedule = "daily_schedule"
    meeting = "meeting"
    leave = "leave"
    payroll = "payroll"
    contract = "contract"
    invoice = "invoice"
    site_progress = "site_progress"
    attendance_alert = "attendance_alert"


class DeliveryStatus(str, Enum):
    pending = "pending"
    sent = "sent"
    skipped = "skipped"
    failed = "failed"
    simulated = "simulated"


class LeaveType(str, Enum):
    personal = "事假"
    sick = "病假"
    annual = "特休"
    official = "公假"
    roster = "排休"
    other = "其他"


class LeaveStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    cancelled = "cancelled"


class AttendanceEventType(str, Enum):
    check_in = "上班打卡"
    check_out = "下班打卡"
    arrive_site = "到達工地"
    leave_site = "離開工地"
    go_out = "外出"
    return_back = "返回"
    overtime_start = "加班開始"
    overtime_end = "加班結束"


class MeetingStatus(str, Enum):
    open = "未完成"
    tracking = "追蹤中"
    closed = "已完成"


class Worksite(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    name: str = Field(index=True, unique=True)
    is_active: bool = Field(default=True)


class Employee(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    employee_code: str = Field(index=True, unique=True)
    name: str
    phone: Optional[str] = None
    line_user_id: Optional[str] = Field(default=None, index=True, unique=True)
    bind_token: str = Field(index=True, unique=True)
    role: Role = Field(default=Role.employee)
    title: Optional[str] = None
    department: Optional[str] = None
    home_site_id: Optional[int] = Field(default=None, foreign_key="worksite.id")
    hire_date: Optional[date] = None
    salary_scheme: Optional[str] = None
    labor_insurance_note: Optional[str] = None
    emergency_contact: Optional[str] = None
    contract_expiry: Optional[date] = None
    licenses: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    training_records: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    machine_skills: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: EmployeeStatus = Field(default=EmployeeStatus.active)


class WorkAssignment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    work_date: date = Field(index=True)
    site_id: int = Field(foreign_key="worksite.id", index=True)
    work_item: str
    supervisor_id: Optional[int] = Field(default=None, foreign_key="employee.id")
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    vehicle: Optional[str] = None
    equipment: Optional[str] = None
    notes: Optional[str] = Field(default=None, sa_column=Column(Text))
    is_completed: bool = False
    status: AssignmentStatus = Field(default=AssignmentStatus.scheduled)
    report_photos: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_by: Optional[int] = Field(default=None, foreign_key="employee.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AssignmentMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    assignment_id: int = Field(foreign_key="workassignment.id", index=True)
    employee_id: int = Field(foreign_key="employee.id", index=True)
    is_active: bool = Field(default=True, index=True)
    replacement_for_employee_id: Optional[int] = Field(default=None, foreign_key="employee.id")
    ack_status: AckStatus = Field(default=AckStatus.pending)
    last_line_action: Optional[str] = None
    note: Optional[str] = None
    photo_url: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class NotificationBatch(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    sender_id: Optional[int] = Field(default=None, foreign_key="employee.id")
    category: NotificationCategory = Field(default=NotificationCategory.ad_hoc)
    target_scope: str
    target_value: Optional[str] = None
    content: str = Field(sa_column=Column(Text))
    assignment_id: Optional[int] = Field(default=None, foreign_key="workassignment.id")
    meeting_id: Optional[int] = Field(default=None, foreign_key="meetingrecord.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class NotificationDelivery(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: int = Field(foreign_key="notificationbatch.id", index=True)
    employee_id: Optional[int] = Field(default=None, foreign_key="employee.id", index=True)
    line_user_id: Optional[str] = None
    delivery_status: DeliveryStatus = Field(default=DeliveryStatus.pending)
    delivery_message: Optional[str] = None
    sent_at: datetime = Field(default_factory=datetime.utcnow)


class LeaveRequest(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    employee_id: int = Field(foreign_key="employee.id", index=True)
    leave_type: str
    start_date: date = Field(index=True)
    end_date: date = Field(index=True)
    reason: str = Field(sa_column=Column(Text))
    status: LeaveStatus = Field(default=LeaveStatus.pending)
    policy_note: Optional[str] = None
    approver_id: Optional[int] = Field(default=None, foreign_key="employee.id")
    review_note: Optional[str] = None
    requested_at: datetime = Field(default_factory=datetime.utcnow)
    reviewed_at: Optional[datetime] = None


class AttendanceEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    employee_id: int = Field(foreign_key="employee.id", index=True)
    site_id: Optional[int] = Field(default=None, foreign_key="worksite.id", index=True)
    assignment_id: Optional[int] = Field(default=None, foreign_key="workassignment.id", index=True)
    event_type: str
    happened_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    source: str = Field(default="line")
    note: Optional[str] = None
    photo_url: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class MeetingRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(index=True)
    meeting_at: datetime
    location: Optional[str] = None
    attendee_codes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    agenda: str = Field(sa_column=Column(Text))
    decisions: str = Field(sa_column=Column(Text))
    owner_id: Optional[int] = Field(default=None, foreign_key="employee.id")
    due_date: Optional[date] = None
    status: MeetingStatus = Field(default=MeetingStatus.open)
    attachment_urls: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    summary: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FinanceEntry(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    entry_date: date = Field(index=True)
    entry_type: str
    category: str
    site_id: Optional[int] = Field(default=None, foreign_key="worksite.id")
    vendor_name: Optional[str] = None
    amount: float
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    payment_status: Optional[str] = None
    payment_method: Optional[str] = None
    handled_by: Optional[str] = None
    attachment_urls: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    note: Optional[str] = Field(default=None, sa_column=Column(Text))


class LineLinkStatus(str, Enum):
    issued = "issued"
    authorized = "authorized"
    completed = "completed"
    failed = "failed"
    expired = "expired"


class LineLinkSession(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    line_user_id: str = Field(index=True)
    link_token: str = Field(index=True, unique=True)
    employee_code: Optional[str] = Field(default=None, index=True)
    bind_token_snapshot: Optional[str] = None
    nonce: Optional[str] = Field(default=None, index=True, unique=True)
    status: LineLinkStatus = Field(default=LineLinkStatus.issued)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    completed_at: Optional[datetime] = None
