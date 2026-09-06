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
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    geofence_radius_m: Optional[int] = None
    is_active: bool = Field(default=True)


class Employee(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    employee_code: str = Field(index=True, unique=True)
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
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
    password_hash: Optional[str] = None
    failed_login_count: int = Field(default=0)
    locked_until: Optional[datetime] = None
    must_change_password: bool = Field(default=False)
    session_key: Optional[str] = None
    session_expires_at: Optional[datetime] = None
    assigned_sites: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: EmployeeStatus = Field(default=EmployeeStatus.active)


class AdminAuditLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    actor_id: Optional[int] = Field(default=None, foreign_key="employee.id", index=True)
    actor_code: str = Field(index=True)
    actor_name: Optional[str] = None
    action: str = Field(index=True)
    entity_type: str = Field(index=True)
    entity_id: Optional[int] = None
    summary: str = Field(sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


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


class PhotoUploadLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # 未綁定員工的 LINE 帳號上傳時也會留下記錄，因此允許為空
    employee_id: Optional[int] = Field(default=None, foreign_key="employee.id", index=True)
    assignment_id: Optional[int] = Field(default=None, foreign_key="workassignment.id", index=True)
    site_id: Optional[int] = Field(default=None, foreign_key="worksite.id", index=True)
    line_user_id: Optional[str] = Field(default=None, index=True)
    source_message_id: Optional[str] = Field(default=None, index=True)
    file_name: str
    drive_file_id: str = Field(index=True)
    drive_folder_id: str
    drive_url: str
    uploaded_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    note: Optional[str] = Field(default=None, sa_column=Column(Text))


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


class LoginStatus(str, Enum):
    success = "success"
    failed = "failed"
    locked = "locked"


class LoginLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    employee_code: Optional[str] = Field(default=None, index=True)
    employee_name: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = Field(default=None, sa_column=Column(Text))
    status: LoginStatus = Field(default=LoginStatus.failed)
    failure_reason: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


# ---- 堆高機出租管理系統 ----

class ForkliftCustomer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    customer_code: str = Field(index=True, unique=True)
    name: str = Field(index=True)
    tax_id: Optional[str] = None
    contact: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    credit_limit: int = Field(default=0)
    payment_terms: Optional[str] = None
    grade: str = Field(default="B")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ForkliftEquipment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    equipment_code: str = Field(index=True, unique=True)
    name: str
    brand: Optional[str] = None
    capacity: float = Field(default=0)
    fuel_type: Optional[str] = None
    status: str = Field(default="可租")
    daily_rate: int = Field(default=0)
    monthly_rate: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ForkliftRental(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    rental_code: str = Field(index=True, unique=True)
    customer_id: int = Field(foreign_key="forkliftcustomer.id", index=True)
    equipment_id: int = Field(foreign_key="forkliftequipment.id", index=True)
    start_date: date = Field(index=True)
    end_date: date = Field(index=True)
    days: int = Field(default=0)
    daily_rate: int = Field(default=0)
    total_amount: int = Field(default=0)
    deposit: int = Field(default=0)
    tax: int = Field(default=0)
    status: str = Field(default="進行中")
    created_at: datetime = Field(default_factory=datetime.utcnow)




class ForkliftStatus(str, Enum):
    operating = "operating"       # 作業中
    available = "available"       # 可調度
    maintenance = "maintenance"   # 待檢修
    inactive = "inactive"         # 停用


class Forklift(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    forklift_code: str = Field(index=True)           # 堆高機編號（1號、2號等）
    model: Optional[str] = None                        # 型號（自排 2.5噸柴油車等）
    status: ForkliftStatus = Field(default=ForkliftStatus.available)
    current_site_id: Optional[int] = Field(default=None, foreign_key="worksite.id")
    current_operator_id: Optional[int] = Field(default=None, foreign_key="employee.id")
    fuel_level: Optional[int] = Field(default=100)    # 油量百分比
    next_maintenance_date: Optional[date] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ForkliftInspection(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    forklift_id: int = Field(foreign_key="forklift.id", index=True)
    operator_id: int = Field(foreign_key="employee.id", index=True)
    site_id: Optional[int] = Field(default=None, foreign_key="worksite.id")
    inspection_date: date = Field(index=True)
    # 點檢項目結果（JSON）：{"engine_oil": true, "coolant": true, ...}
    inspection_items: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    all_passed: bool = Field(default=True)
    notes: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MasterOptionType(str, Enum):
    work_item = "work_item"   # 派工工作內容
    equipment = "equipment"   # 機具設備


class MasterOption(SQLModel, table=True):
    """後台可維護的點選選項（工作內容、機具設備），讓派工表單可點選且能自行新增。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    option_type: str = Field(index=True)   # 對應 MasterOptionType
    label: str
    sort_order: int = Field(default=0)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
