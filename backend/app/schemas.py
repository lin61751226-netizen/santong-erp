from datetime import date, datetime, time
from typing import Optional

from pydantic import BaseModel, Field

from app.models import LeaveStatus, MeetingStatus, NotificationCategory


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
