from datetime import date, datetime, time
from typing import Optional

from pydantic import BaseModel, Field

from app.models import LeaveStatus, MeetingStatus, NotificationCategory, Role


class AssignmentCreate(BaseModel):
    work_date: date
    site_id: int
    work_item: str
    supervisor_code: Optional[str] = None
    employee_codes: list[str]
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    vehicle: Optional[str] = None
    equipment: Optional[str] = None
    notes: Optional[str] = None


class EmployeeUpdate(BaseModel):
    title: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    assigned_sites: list[str] = Field(default_factory=list)


class EmployeeCreate(BaseModel):
    employee_code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=100)
    role: Role = Role.employee
    title: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    assigned_sites: list[str] = Field(default_factory=list)


class ForkliftCreate(BaseModel):
    forklift_code: str = Field(min_length=1, max_length=50)
    model: Optional[str] = None
    site_id: Optional[int] = None
    fuel_level: Optional[int] = Field(default=100, ge=0, le=100)
    next_maintenance_date: Optional[date] = None


class WorksiteCreate(BaseModel):
    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=100)
    address: Optional[str] = None
    google_maps_url: Optional[str] = None
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    geofence_radius_m: Optional[int] = Field(default=None, ge=10, le=10000)


class WorksiteLocationUpdate(BaseModel):
    address: Optional[str] = None
    google_maps_url: Optional[str] = None
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    geofence_radius_m: Optional[int] = Field(default=None, ge=10, le=10000)


class MasterOptionCreate(BaseModel):
    # option_type: work_item（工作內容）/ equipment（機具設備）
    option_type: str
    label: str


class LoginRequest(BaseModel):
    employee_code: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class PasswordResetRequest(BaseModel):
    employee_code: str
    new_password: str


class NotificationCreate(BaseModel):
    category: NotificationCategory = NotificationCategory.ad_hoc
    target_scope: str = Field(description="all/site/department/employee/management/supervisor")
    target_value: Optional[str] = None
    content: str
    assignment_id: Optional[int] = None
    meeting_id: Optional[int] = None


class LeaveRequestCreate(BaseModel):
    employee_code: Optional[str] = None
    leave_type: str
    start_date: date
    end_date: date
    reason: str


class LeaveDecision(BaseModel):
    status: LeaveStatus
    review_note: Optional[str] = None


class MeetingCreate(BaseModel):
    title: str
    meeting_at: datetime
    location: Optional[str] = None
    attendee_codes: list[str] = Field(default_factory=list)
    agenda: str
    decisions: str
    owner_code: Optional[str] = None
    due_date: Optional[date] = None
    status: MeetingStatus = MeetingStatus.open
    summary: Optional[str] = None
    send_summary: bool = False


class SimulateLineMessage(BaseModel):
    line_user_id: str = "U-demo-user"
    message: str


class ReassignmentApplyRequest(BaseModel):
    absent_employee_code: str
    replacement_employee_code: str
    leave_request_id: Optional[int] = None
    notify_replacement: bool = True


class LineWebhookConfigureRequest(BaseModel):
    base_url: Optional[str] = None
    test_after_set: bool = True


class LineRichMenuDeployRequest(BaseModel):
    base_url: Optional[str] = None
    set_default: bool = True


# ---- 堆高機出租管理 ----

class ForkliftCareUpdate(BaseModel):
    site_id: Optional[int] = None
    fuel_level: Optional[int] = Field(default=None, ge=0, le=100)
    next_maintenance_date: Optional[date] = None


class ForkliftCustomerCreate(BaseModel):
    name: str
    tax_id: Optional[str] = None
    contact: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    credit_limit: int = 0
    payment_terms: Optional[str] = None
    grade: str = "B"


class ForkliftCustomerUpdate(BaseModel):
    name: Optional[str] = None
    tax_id: Optional[str] = None
    contact: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    credit_limit: Optional[int] = None
    payment_terms: Optional[str] = None
    grade: Optional[str] = None


class ForkliftEquipmentCreate(BaseModel):
    name: str
    brand: Optional[str] = None
    capacity: float = 0
    fuel_type: Optional[str] = None
    status: str = "可租"
    daily_rate: int = 0
    monthly_rate: int = 0


class ForkliftEquipmentUpdate(BaseModel):
    name: Optional[str] = None
    brand: Optional[str] = None
    capacity: Optional[float] = None
    fuel_type: Optional[str] = None
    status: Optional[str] = None
    daily_rate: Optional[int] = None
    monthly_rate: Optional[int] = None


class ForkliftRentalCreate(BaseModel):
    customer_id: int
    equipment_id: int
    start_date: str
    end_date: str
    days: int
    daily_rate: int
    total_amount: int
    deposit: int
    tax: int
    status: str = "進行中"


class ForkliftRentalUpdate(BaseModel):
    customer_id: Optional[int] = None
    equipment_id: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    days: Optional[int] = None
    daily_rate: Optional[int] = None
    total_amount: Optional[int] = None
    deposit: Optional[int] = None
    tax: Optional[int] = None
    status: Optional[str] = None
