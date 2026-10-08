from datetime import date
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import AdminAuditLog, Employee, EmployeeStatus, LeaveRequest, LeaveStatus, Role, Worksite
from app.services.management_reports import report


@pytest.fixture
def ctx(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        admin = Employee(employee_code='ADMIN001', name='管理員', bind_token='admin', role=Role.admin, must_change_password=False)
        worker = Employee(employee_code='EMP001', name='操作員', bind_token='worker', line_user_id='U-preserved', password_hash='unchanged')
        site = Worksite(code='53', name='齊裕53')
        session.add_all([admin, worker, site]); session.commit()
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_actor] = lambda: admin
        backup = AsyncMock(return_value={'status': 'saved'})
        notify = AsyncMock()
        monkeypatch.setattr('app.routes.admin.google_drive_worklog_service.backup_database', backup)
        monkeypatch.setattr('app.routes.admin.notify_employees', notify)
        monkeypatch.setattr('app.routes.admin.local_today', lambda: date(2026, 10, 8))
        client = TestClient(app)
        yield client, session, worker, site, backup, notify
        client.close()
        app.dependency_overrides.clear()
    engine.dispose()


def payload(**changes):
    return {"employee_code": "EMP001", "leave_type": "排休", "start_date": "2026-11-02",
            "end_date": "2026-11-02", "reason": "  月初排休  ", **changes}


def test_save_pending_audited_and_backup_runs_after_commit(ctx):
    client, session, worker, _, backup, notify = ctx

    async def verify_saved():
        assert len(session.exec(select(LeaveRequest)).all()) == 1
        assert len(session.exec(select(AdminAuditLog)).all()) == 1
        return {'status': 'saved'}

    backup.side_effect = verify_saved
    result = client.post('/api/leave-requests', json=payload())
    assert result.status_code == 201
    assert result.json()['backup_status'] == 'saved'
    leave = session.get(LeaveRequest, result.json()['leave_request_id'])
    assert leave.status == LeaveStatus.pending
    assert leave.reason == '月初排休'
    assert worker.line_user_id == 'U-preserved' and worker.password_hash == 'unchanged'
    notify.assert_not_awaited()
    assert client.post('/api/leave-requests', json=payload()).status_code == 400
    assert len(session.exec(select(LeaveRequest)).all()) == 1
    backup.assert_awaited_once()


@pytest.mark.parametrize('changes', [
    {'leave_type': '未知假別'}, {'reason': '  '}, {'reason': 'a' * 2001},
    {'end_date': '2026-11-01'}, {'end_date': '2026-11-08'},
    {'end_date': '2026-12-01'}, {'start_date': '2026-10-08', 'end_date': '2026-10-08'},
])
def test_rejects_invalid_inputs_and_roster_policy(ctx, changes):
    client, session, _, _, backup, _ = ctx
    assert client.post('/api/leave-requests', json=payload(**changes)).status_code == 400
    assert not session.exec(select(LeaveRequest)).all()
    assert not session.exec(select(AdminAuditLog)).all()
    backup.assert_not_awaited()


def test_inactive_employee_and_nonexistent_code_are_rejected(ctx):
    client, session, worker, _, backup, _ = ctx
    worker.status = EmployeeStatus.inactive
    session.add(worker); session.commit()
    assert client.post('/api/leave-requests', json=payload()).status_code == 400
    assert client.post('/api/leave-requests', json=payload(employee_code='MISSING')).status_code == 404
    backup.assert_not_awaited()


def test_backup_failure_is_not_misreported_as_saved(ctx):
    client, session, _, _, backup, _ = ctx
    backup.return_value = {'status': 'failed'}
    result = client.post('/api/leave-requests', json=payload())
    assert result.status_code == 201
    assert result.json()['backup_status'] == 'failed'
    assert len(session.exec(select(LeaveRequest)).all()) == 1


def test_approval_audits_backs_up_and_links_to_calendar_report_and_assignment_guard(ctx):
    client, session, worker, site, backup, notify = ctx
    created = client.post('/api/leave-requests', json=payload())
    leave_id = created.json()['leave_request_id']
    assert all('核准排休' not in str(row) for row in report(session, None, '2026-11', 'roster')['rows'])
    decision = client.post(f'/api/leave-requests/{leave_id}/decision', json={'status': 'approved', 'review_note': '核准'})
    assert decision.status_code == 200 and decision.json()['backup_status'] == 'saved'
    assert session.get(LeaveRequest, leave_id).status == LeaveStatus.approved
    assert backup.await_count == 2 and notify.await_count == 1
    assert len(session.exec(select(AdminAuditLog)).all()) == 2
    assert any('核准排休' in str(row) for row in report(session, None, '2026-11', 'roster')['rows'])
    calendar = client.get('/api/calendar?start_date=2026-11-01&end_date=2026-11-30').json()
    assert any(item['type'] == 'leave' for item in calendar['events'])
    assignment = client.post('/api/assignments', json={'work_date': '2026-11-02', 'site_id': site.id,
        'work_item': '移料', 'employee_codes': [worker.employee_code]})
    assert assignment.status_code == 400
    assert worker.line_user_id == 'U-preserved' and worker.password_hash == 'unchanged'


def test_report_ui_does_not_bypass_session_auth(ctx):
    client, _, _, _, backup, _ = ctx
    del app.dependency_overrides[get_current_actor]
    assert client.post('/api/leave-requests', json=payload()).status_code == 401
    backup.assert_not_awaited()
