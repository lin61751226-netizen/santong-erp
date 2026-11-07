from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import AdminAuditLog, Employee, Role, WorksiteForkliftCharge
from app.routes import worksite_pricing as routes
from app.services.bootstrap import _ensure_guotai_ciwen_rates, _ensure_worksites


@pytest.fixture
def ctx():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    site = _ensure_worksites(session)['國泰慈文']
    _ensure_guotai_ciwen_rates(session, {'國泰慈文': site})
    actor = Employee(employee_code='ADMIN001', name='管理員', bind_token='test', role=Role.admin)
    session.add(actor)
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_actor] = lambda: actor
    backup = AsyncMock(return_value={'status': 'saved'})
    with patch.object(routes.drive, 'backup_database', backup):
        yield SimpleNamespace(client=TestClient(app), session=session, site=site, actor=actor, backup=backup)
    app.dependency_overrides.clear()
    session.close()
    engine.dispose()


def row_payload(ctx, day='2026-11-02', vehicle='2.5噸（2節桿）', **changes):
    return {'worksite_id': ctx.site.id, 'work_date': day, 'vehicle_type': vehicle,
            'units': 1, 'normal_hours': 8, 'overtime_hours': 2, 'holiday_reason': '',
            'external_customer': '', 'fuel_cost': 100, 'payer': '',
            'expected_revision': 0, 'request_id': 'row-test-0001', **changes}


def test_november_start_preview_save_and_revision(ctx):
    card = ctx.client.get(f'/api/worksites/{ctx.site.id}/forklift-rates')
    assert card.status_code == 200 and card.json()['rates'][0]['effective_from'] == '2026-11-01'
    october = ctx.client.get('/api/worksite-pricing/month', params={'worksite_id': ctx.site.id, 'year': 2026, 'month': 10})
    assert october.status_code == 422
    november = ctx.client.get('/api/worksite-pricing/month', params={'worksite_id': ctx.site.id, 'year': 2026, 'month': 11})
    assert november.status_code == 200
    assert len(november.json()['sheets']) == 2
    assert len(november.json()['sheets'][0]['rows']) == 30
    payload = row_payload(ctx)
    preview = ctx.client.post('/api/worksite-pricing/row', json={**payload, 'preview_only': True})
    assert preview.status_code == 200 and preview.json()['total_amount'] == 11000
    assert not ctx.session.exec(select(WorksiteForkliftCharge)).all()
    assert ctx.client.post('/api/worksite-pricing/row', json=payload).status_code == 409
    saved = ctx.client.post('/api/worksite-pricing/row', json={**payload, 'confirmed': True})
    assert saved.status_code == 200 and saved.json()['backup']['status'] == 'saved'
    assert saved.json()['revision'] == 1
    assert ctx.client.post('/api/worksite-pricing/row', json={**payload, 'confirmed': True}).json()['replayed']
    assert ctx.client.post('/api/worksite-pricing/row', json={**payload, 'confirmed': True,
          'request_id': 'row-test-0002'}).status_code == 409
    assert len(ctx.session.exec(select(WorksiteForkliftCharge)).all()) == 1
    assert ctx.session.exec(select(AdminAuditLog).where(AdminAuditLog.action == 'worksite_pricing_edit')).first()


def test_holiday_and_effective_date_validation(ctx):
    assert ctx.client.post('/api/worksite-pricing/row', json=row_payload(ctx, day='2026-10-31')).status_code == 422
    assert ctx.client.post('/api/worksite-pricing/row', json=row_payload(ctx, day='2026-11-01')).status_code == 422
    sunday = ctx.client.post('/api/worksite-pricing/row', json=row_payload(
        ctx, day='2026-11-01', holiday_reason='週日', preview_only=True))
    assert sunday.status_code == 200 and sunday.json()['total_amount'] == 13000
    spring = ctx.client.post('/api/worksite-pricing/row', json=row_payload(
        ctx, day='2026-11-02', holiday_reason='春節', preview_only=True))
    assert spring.status_code == 200 and spring.json()['total_amount'] == 13000
    assert ctx.client.post('/api/worksite-pricing/row', json=row_payload(ctx, normal_hours=9)).status_code == 422


def test_worksite_pricing_employee_denied(ctx):
    ctx.actor.role = Role.employee
    ctx.session.add(ctx.actor)
    ctx.session.commit()
    assert ctx.client.get('/api/worksite-pricing/month', params={'worksite_id': ctx.site.id, 'year': 2026, 'month': 11}).status_code == 403
    assert ctx.client.post('/api/worksite-pricing/row', json=row_payload(ctx)).status_code == 403


def test_inactive_site_keeps_history_readable_but_blocks_new_price(ctx):
    ctx.site.is_active = False
    ctx.session.add(ctx.site)
    ctx.session.commit()
    assert ctx.client.get('/api/worksite-pricing/month', params={
        'worksite_id': ctx.site.id, 'year': 2026, 'month': 11,
    }).status_code == 200
    assert ctx.client.post('/api/worksite-pricing/row', json=row_payload(ctx)).status_code == 409
