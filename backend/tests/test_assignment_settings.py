import asyncio
import json
from contextlib import contextmanager
from datetime import date, time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import (
    AckStatus, AdminAuditLog, AssignmentMember, AttendanceEvent, Employee,
    EmployeeStatus, LeaveRequest, LeaveStatus, Role, WorkAssignment, WorkReportEvent, Worksite,
)
from app.services.hr import find_assignment_for_employee


@pytest.fixture
def ctx(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        admin = Employee(employee_code='ADMIN001', name='管理員', bind_token='a', role=Role.admin, must_change_password=False)
        worker = Employee(employee_code='EMP001', name='原操作員', bind_token='w', line_user_id='U-kept', password_hash='kept')
        other = Employee(employee_code='EMP002', name='候補', bind_token='o')
        sites = [Worksite(code='53', name='齊裕53'), Worksite(code='47', name='善捷47')]
        session.add_all([admin, worker, other, *sites]); session.commit()
        assignment = WorkAssignment(work_date=date(2026, 11, 2), site_id=sites[0].id, work_item='原工作',
            supervisor_id=admin.id, start_time=time(8), end_time=time(17), vehicle='2.5T×1', equipment='堆高機', created_by=admin.id)
        session.add(assignment); session.commit()
        member = AssignmentMember(assignment_id=assignment.id, employee_id=worker.id)
        session.add(member); session.commit()
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_actor] = lambda: admin
        backup = AsyncMock(return_value={'status': 'saved'})
        monkeypatch.setattr('app.routes.admin.google_drive_worklog_service.backup_database', backup)
        client = TestClient(app)
        yield client, session, admin, worker, other, sites, assignment, member, backup
        client.close(); app.dependency_overrides.clear()
    engine.dispose()


def edit_payload(client, assignment, **changes):
    row = client.get(f'/api/assignments/{assignment.id}').json()
    return {key: row.get(key) for key in ('work_date', 'site_id', 'work_item', 'supervisor_code', 'start_time',
        'end_time', 'vehicle', 'equipment', 'notes', 'version')} | {'employee_codes': row['member_codes']} | changes


def test_cancel_preserves_history_excludes_schedule_and_rejects_old_editor(ctx, monkeypatch):
    from app.services import scheduler
    client, session, admin, worker, _, sites, assignment, member, backup = ctx
    assignment.work_date = date.today()
    member.ack_status = AckStatus.arrived
    event = AttendanceEvent(employee_id=worker.id, site_id=sites[0].id, assignment_id=assignment.id, event_type='check_in')
    session.add_all([assignment, member, event]); session.commit()
    payload = edit_payload(client, assignment)
    result = client.post(f'/api/assignments/{assignment.id}/cancel', json={'version': payload['version']})
    assert result.status_code == 200 and result.json()['backup_status'] == 'saved'
    assert assignment.status.value == 'cancelled' and not member.is_active
    assert session.get(AttendanceEvent, event.id) and member.ack_status == AckStatus.arrived
    assert worker.line_user_id == 'U-kept' and worker.password_hash == 'kept'
    assert find_assignment_for_employee(session, worker.id, date.today()) is None
    assert client.patch(f'/api/assignments/{assignment.id}', json=payload).status_code == 409
    assert client.patch(f'/api/assignments/{assignment.id}', json=edit_payload(client, assignment)).status_code == 409
    assert any(r['status'] == 'cancelled' for r in client.get('/api/assignments').json())
    journal = client.get(f'/api/worksite-journals?target_date={date.today()}').json()
    assert not any(site['assignments'] for site in journal['sites'])
    audit = session.exec(select(AdminAuditLog)).one()
    assert audit.action == 'cancel' and json.loads(audit.summary)['before']['members'][0]['is_active']
    @contextmanager
    def scope():
        yield session
    monkeypatch.setattr(scheduler, 'session_scope', scope)
    notify = AsyncMock(); monkeypatch.setattr(scheduler, 'notify_employees', notify)
    asyncio.run(scheduler.push_daily_assignments())
    notify.assert_not_awaited()
    backup.return_value = {'status': 'failed'}
    version = client.get(f'/api/assignments/{assignment.id}').json()['version']
    repeated = client.post(f'/api/assignments/{assignment.id}/cancel', json={'version': version})
    assert repeated.json()['backup_status'] == 'failed'
    assert len(session.exec(select(AdminAuditLog)).all()) == 1


def test_cancel_checks_role_scope_and_version(ctx):
    client, session, admin, worker, _, sites, assignment, member, backup = ctx
    url = f'/api/assignments/{assignment.id}/cancel'
    payload = {'version': edit_payload(client, assignment)['version']}
    app.dependency_overrides[get_current_actor] = lambda: worker
    assert client.post(url, json=payload).status_code == 403
    admin.role = Role.site_manager; admin.home_site_id = sites[1].id
    app.dependency_overrides[get_current_actor] = lambda: admin
    assert client.post(url, json=payload).status_code == 403
    admin.home_site_id = sites[0].id
    assert client.post(url, json={'version': '0' * 64}).status_code == 409
    assert assignment.status.value == 'scheduled' and member.is_active
    backup.assert_not_awaited()


def test_modify_all_settings_and_audit_backup_after_atomic_commit(ctx):
    client, session, admin, worker, other, sites, assignment, member, backup = ctx
    before_created = assignment.created_at
    payload = edit_payload(client, assignment, work_date='2026-11-03', site_id=sites[1].id, work_item=' 新工作 ',
        supervisor_code=None, employee_codes=[other.employee_code], start_time='09:00', end_time='18:00',
        vehicle='3T×2', equipment='堆高機2台', notes='新注意事項')

    async def verify_committed():
        assert session.get(WorkAssignment, assignment.id).work_item == '新工作'
        assert len(session.exec(select(AdminAuditLog)).all()) == 1
        return {'status': 'saved'}

    backup.side_effect = verify_committed
    result = client.patch(f'/api/assignments/{assignment.id}', json=payload)
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved['backup_status'] == 'saved' and saved['changed']
    assert saved['assignment']['member_codes'] == [other.employee_code]
    assert assignment.id == saved['assignment_id'] and assignment.created_at == before_created
    assert assignment.created_by == admin.id and not member.is_active
    assert worker.line_user_id == 'U-kept' and worker.password_hash == 'kept'
    assert len(session.exec(select(WorkAssignment)).all()) == 1
    audit = json.loads(session.exec(select(AdminAuditLog)).first().summary)
    assert audit['before']['assignment']['work_item'] == '原工作'
    assert audit['after']['assignment']['work_item'] == '新工作'
    assert find_assignment_for_employee(session, worker.id, date(2026, 11, 3)) is None
    assert find_assignment_for_employee(session, other.id, date(2026, 11, 3)).id == assignment.id
    calendar = client.get('/api/calendar?start_date=2026-11-01&end_date=2026-11-30').json()
    event = next(item for item in calendar['events'] if item['type'] == 'assignment')
    assert event['date'] == '2026-11-03' and '1 人' in event['detail']
    journal = client.get('/api/worksite-journals?target_date=2026-11-03').json()
    assert any(row['work_item'] == '新工作' for site in journal['sites'] for row in site['assignments'])
    backup.assert_awaited_once()


def test_stale_version_rejected_and_same_payload_does_not_duplicate_audit(ctx):
    client, session, _, _, _, _, assignment, _, backup = ctx
    payload = edit_payload(client, assignment, notes='修改')
    assert client.patch(f'/api/assignments/{assignment.id}', json=payload).status_code == 200
    stale = client.patch(f'/api/assignments/{assignment.id}', json=payload)
    assert stale.status_code == 409
    current = edit_payload(client, assignment)
    result = client.patch(f'/api/assignments/{assignment.id}', json=current)
    assert result.status_code == 200 and result.json()['changed'] is False
    assert len(session.exec(select(AdminAuditLog)).all()) == 1
    assert backup.await_count == 2


@pytest.mark.parametrize('changes', [
    {'work_item': '  '}, {'work_item': 'x' * 2001}, {'notes': 'x' * 4001}, {'vehicle': 'x' * 501},
    {'employee_codes': []}, {'employee_codes': ['MISSING']}, {'site_id': 999},
    {'supervisor_code': 'EMP002'}, {'end_time': '07:00'},
])
def test_validation_leaves_original_unchanged(ctx, changes):
    client, session, _, worker, _, _, assignment, member, backup = ctx
    result = client.patch(f'/api/assignments/{assignment.id}', json=edit_payload(client, assignment, **changes))
    assert result.status_code in {400, 404}
    assert assignment.work_item == '原工作' and member.is_active
    assert not session.exec(select(AdminAuditLog)).all()
    assert worker.line_user_id == 'U-kept'
    backup.assert_not_awaited()


def test_approved_leave_blocks_replacement(ctx):
    client, session, _, _, other, _, assignment, _, backup = ctx
    session.add(LeaveRequest(employee_id=other.id, leave_type='排休', start_date=assignment.work_date,
        end_date=assignment.work_date, reason='休假', status=LeaveStatus.approved)); session.commit()
    result = client.patch(f'/api/assignments/{assignment.id}', json=edit_payload(client, assignment, employee_codes=[other.employee_code]))
    assert result.status_code == 400 and '已核准請假' in result.json()['detail']
    backup.assert_not_awaited()


@pytest.mark.parametrize('history_model', [AttendanceEvent, WorkReportEvent])
def test_historical_events_never_relabel_or_disappear(ctx, history_model):
    client, session, _, worker, _, sites, assignment, member, backup = ctx
    event = history_model(employee_id=worker.id, site_id=sites[0].id, assignment_id=assignment.id,
        event_type='check_in' if history_model is AttendanceEvent else 'started')
    session.add(event); session.commit()
    payload = edit_payload(client, assignment, site_id=sites[1].id)
    assert client.patch(f'/api/assignments/{assignment.id}', json=payload).status_code == 409
    payload = edit_payload(client, assignment, notes='仍可修改注意事項')
    assert client.patch(f'/api/assignments/{assignment.id}', json=payload).status_code == 200
    preserved = session.get(history_model, event.id)
    assert preserved.site_id == sites[0].id and preserved.assignment_id == assignment.id and member.is_active


def test_member_ack_retained_when_removed_and_restored(ctx):
    client, session, _, worker, other, _, assignment, member, _ = ctx
    member.ack_status = AckStatus.arrived; member.note = '到場'; session.add(member); session.commit()
    assert client.patch(f'/api/assignments/{assignment.id}', json=edit_payload(client, assignment,
        employee_codes=[other.employee_code])).status_code == 200
    assert not member.is_active and member.ack_status == AckStatus.arrived
    assert client.patch(f'/api/assignments/{assignment.id}', json=edit_payload(client, assignment,
        employee_codes=[worker.employee_code])).status_code == 200
    assert member.is_active and member.note == '到場' and member.ack_status == AckStatus.arrived
    assert len(session.exec(select(AssignmentMember)).all()) == 2


def test_inactive_members_or_site_retained_but_cannot_be_newly_selected(ctx):
    client, session, _, worker, other, sites, assignment, _, _ = ctx
    worker.status = other.status = EmployeeStatus.inactive
    sites[0].is_active = sites[1].is_active = False
    session.add_all([worker, other, *sites]); session.commit()
    assert client.patch(f'/api/assignments/{assignment.id}', json=edit_payload(client, assignment, notes='修改')).status_code == 200
    assert client.patch(f'/api/assignments/{assignment.id}', json=edit_payload(client, assignment, site_id=sites[1].id)).status_code == 400
    assert client.patch(f'/api/assignments/{assignment.id}', json=edit_payload(client, assignment, employee_codes=[other.employee_code])).status_code == 400


def test_backup_failure_returns_saved_record_and_not_false_success(ctx):
    client, session, _, _, _, _, assignment, _, backup = ctx
    backup.return_value = {'status': 'failed'}
    result = client.patch(f'/api/assignments/{assignment.id}', json=edit_payload(client, assignment, notes='已存'))
    assert result.status_code == 200 and result.json()['backup_status'] == 'failed'
    assert session.get(WorkAssignment, assignment.id).notes == '已存'


def test_auth_role_and_both_site_scopes(ctx):
    client, session, admin, worker, _, sites, assignment, _, backup = ctx
    payload = edit_payload(client, assignment, notes='新')
    app.dependency_overrides[get_current_actor] = lambda: worker
    assert client.get(f'/api/assignments/{assignment.id}').status_code == 403
    assert client.patch(f'/api/assignments/{assignment.id}', json=payload).status_code == 403
    admin.role = Role.site_manager; admin.home_site_id = sites[1].id
    app.dependency_overrides[get_current_actor] = lambda: admin
    assert client.patch(f'/api/assignments/{assignment.id}', json=payload).status_code == 403
    admin.home_site_id = sites[0].id
    assert client.patch(f'/api/assignments/{assignment.id}', json={**payload, 'site_id': sites[1].id}).status_code == 403
    del app.dependency_overrides[get_current_actor]
    assert client.get(f'/api/assignments/{assignment.id}').status_code == 401
    assert client.patch(f'/api/assignments/{assignment.id}', json=payload).status_code == 401
    backup.assert_not_awaited()


def test_create_is_atomic_audited_and_backed_up(ctx):
    client, session, _, _, other, sites, _, _, backup = ctx
    result = client.post('/api/assignments', json={'work_date': '2026-11-04', 'site_id': sites[0].id,
        'work_item': '移料', 'employee_codes': [other.employee_code, other.employee_code]})
    assert result.status_code == 201 and result.json()['backup_status'] == 'saved'
    created = result.json()['assignment']
    assert created['member_codes'] == [other.employee_code]
    assert len(session.exec(select(AdminAuditLog)).all()) == 1
    backup.assert_awaited_once()


def test_daily_push_only_uses_current_members(ctx, monkeypatch):
    from app.services import scheduler
    _, session, _, worker, other, _, assignment, member, _ = ctx
    assignment.work_date = date.today(); member.is_active = False
    session.add_all([assignment, member, AssignmentMember(assignment_id=assignment.id, employee_id=other.id)]); session.commit()
    @contextmanager
    def scope():
        yield session
    monkeypatch.setattr(scheduler, 'session_scope', scope)
    notify = AsyncMock(); monkeypatch.setattr(scheduler, 'notify_employees', notify)
    asyncio.run(scheduler.push_daily_assignments())
    assert [item.id for item in notify.call_args.kwargs['employees']] == [other.id]
