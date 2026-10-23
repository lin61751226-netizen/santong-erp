from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.main import app
from app.core.db import get_session
from app.deps import get_current_actor
from app.models import AdminAuditLog, Employee, InvoicePrintRevision, Role
from app.services.google_drive import PRESERVED_DATABASE_TABLES, google_drive_worklog_service as drive
from app.services.invoice_print import PrintDraft, PrintLayout, calculate, uppercase_twd


@pytest.fixture
def ctx():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        actor = Employee(employee_code='TEST', name='測試', role=Role.admin, bind_token='keep',
                         password_hash='unchanged', line_user_id='keep-line')
        session.add(actor); session.commit()
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_actor] = lambda: actor
        with patch.object(drive, 'backup_database', AsyncMock(return_value={'status': 'saved'})) as backup:
            yield TestClient(app), session, actor, backup
        app.dependency_overrides.clear()
    engine.dispose()


def draft():
    return {'buyer': '測試買受人', 'tax_id': '12345678', 'invoice_date': '2026-10-23',
            'items': [{'description': '堆高機作業', 'quantity': '3', 'unit': '台', 'unit_price': '6000'}], 'tax': 900}


@pytest.mark.parametrize('value,expected', [
    (0,'零元整'), (10,'壹拾元整'), (101,'壹佰零壹元整'), (1001,'壹仟零壹元整'),
    (10000,'壹萬元整'), (10001,'壹萬零壹元整'), (10010,'壹萬零壹拾元整'),
    (18900,'壹萬捌仟玖佰元整'), (100000001,'壹億零壹元整'),
    (100010001,'壹億零壹萬零壹元整'), (123456789,'壹億貳仟參佰肆拾伍萬陸仟柒佰捌拾玖元整'),
])
def test_uppercase(value, expected):
    assert uppercase_twd(value) == expected


@pytest.mark.parametrize('value', [-1, 1.2, True, 1000000000000])
def test_uppercase_invalid(value):
    with pytest.raises(ValueError):
        uppercase_twd(value)


def test_decimal_rounding_and_roc():
    payload = draft(); payload['items'][0].update(quantity='1.5', unit_price='100.33'); payload['tax'] = 0
    assert calculate(PrintDraft(**payload)) == {'amounts': [150], 'subtotal': 150, 'tax': 0,
        'total': 150, 'uppercase': '壹佰伍拾元整', 'roc_year': 115}
    payload['items'][0]['unit_price'] = '101'
    assert calculate(PrintDraft(**payload))['total'] == 152


def test_default_layout_get_is_read_only(ctx):
    client, session, actor, backup = ctx
    response = client.get('/api/invoice-print/template')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    data = response.json(); assert data['revision'] == 0
    assert data['content']['fields']['buyer']['x'] == 27
    assert data['content']['fields']['uppercase']['y'] == 82
    assert session.exec(select(InvoicePrintRevision)).all() == []
    assert not backup.called
    assert client.get('/invoice-overprint').headers['cache-control'] == 'no-store'


def test_layout_revision_retention_conflict_and_identity(ctx):
    client, session, actor, backup = ctx
    content = PrintLayout().model_dump(); content['offset_x'] = 1; content['offset_y'] = -2
    payload = {'expected_revision': 0, 'content': content}
    result = client.put('/api/invoice-print/template', json=payload)
    assert result.status_code == 200 and result.json()['backup']['status'] == 'saved'
    assert client.get('/api/invoice-print/template').json()['content']['offset_y'] == -2
    assert client.put('/api/invoice-print/template', json=payload).status_code == 409
    payload['expected_revision'] = 1; payload['content']['font_size'] = 12
    assert client.put('/api/invoice-print/template', json=payload).json()['revision'] == 2
    assert len(session.exec(select(InvoicePrintRevision)).all()) == 2
    assert len(session.exec(select(AdminAuditLog)).all()) == 2
    session.refresh(actor)
    assert (actor.password_hash, actor.line_user_id, actor.bind_token) == ('unchanged','keep-line','keep')
    assert 'invoiceprintrevision' in PRESERVED_DATABASE_TABLES


def test_draft_history_no_finance_writes(ctx):
    client, session, actor, backup = ctx
    from app.models import FinanceEntry, WorkHourImportLog, SignSlipRecord
    payload = {'record_key': 'test-draft', 'expected_revision': 0, 'content': draft()}
    assert client.post('/api/invoice-print/calculate', json=draft()).json()['total'] == 18900
    assert not backup.called
    assert client.post('/api/invoice-print/drafts', json=payload).status_code == 200
    payload['expected_revision'] = 1; payload['content']['buyer'] = '修改抬頭'
    assert client.post('/api/invoice-print/drafts', json=payload).json()['revision'] == 2
    assert client.post('/api/invoice-print/drafts', json=payload).status_code == 409
    items = client.get('/api/invoice-print/drafts').json()['items']
    assert len(items) == 1 and items[0]['content']['buyer'] == '修改抬頭'
    assert len(session.exec(select(InvoicePrintRevision)).all()) == 2
    for model in (FinanceEntry, WorkHourImportLog, SignSlipRecord):
        assert session.exec(select(model)).all() == []


@pytest.mark.parametrize('key,value', [('font','url(https://bad)'),('offset_x',21),('font_size',0),('row_pitch',30),('tax_id_spacing',-1),('offset_y',float('inf'))])
def test_bad_layout_rejected(ctx, key, value):
    client, session, actor, backup = ctx
    content = PrintLayout().model_dump(); content[key] = value
    if value == float('inf'):
        with pytest.raises(ValueError): PrintLayout(**content)
        return
    assert client.put('/api/invoice-print/template', json={'expected_revision':0,'content':content}).status_code == 422
    assert not backup.called


def test_field_shape_and_outside_width_rejected(ctx):
    client, *_ = ctx
    for fields in ({}, {**PrintLayout().model_dump()['fields'], 'other': {'x':0,'y':0,'width':1}}):
        content = PrintLayout().model_dump();content['fields'] = fields
        assert client.put('/api/invoice-print/template', json={'expected_revision':0,'content':content}).status_code == 422
    content = PrintLayout().model_dump();content['fields']['buyer']['width'] = 190
    assert client.put('/api/invoice-print/template', json={'expected_revision':0,'content':content}).status_code == 422


@pytest.mark.parametrize('change', [{'tax_id':'123'}, {'invoice_date':'2026-02-30'}, {'invoice_date':'1911-01-01'}, {'tax':-1}, {'tax':1.5}, {'items':[]}, {'items':draft()['items']*6}, {'items':[{'description':'   ','unit_price':'1'}]}])
def test_bad_draft(ctx, change):
    client, *_ = ctx
    payload = {**draft(), **deepcopy(change)}
    assert client.post('/api/invoice-print/calculate', json=payload).status_code == 422


def test_amount_ceiling(ctx):
    client, *_ = ctx
    payload=draft();payload['items'][0].update(quantity='1000000',unit_price='999999999')
    assert client.post('/api/invoice-print/calculate', json=payload).status_code == 422


def test_backup_failure_honest_and_retry(ctx):
    client, session, actor, backup = ctx
    backup.side_effect = RuntimeError('private diagnostic')
    result = client.post('/api/invoice-print/drafts', json={'record_key':'x','expected_revision':0,'content':draft()})
    assert result.status_code == 200 and result.json()['backup']['status'] == 'failed'
    assert len(session.exec(select(InvoicePrintRevision)).all()) == 1
    backup.side_effect=None;backup.return_value={'status':'saved'}
    assert client.post('/api/invoice-print/backup').json()['status'] == 'saved'


def test_private_and_roles(ctx):
    client, session, actor, backup = ctx
    actor.role = Role.employee
    assert client.get('/invoice-overprint').status_code == 403
    assert client.get('/api/invoice-print/template').status_code == 403
    app.dependency_overrides.pop(get_current_actor)
    for path in ['/invoice-overprint','/api/invoice-print/template','/api/invoice-print/drafts']:
        assert client.get(path).status_code == 401
    assert client.post('/api/invoice-print/calculate',json=draft()).status_code == 401
    assert client.put('/api/invoice-print/template',json={'expected_revision':0,'content':PrintLayout().model_dump()}).status_code == 401
    assert client.post('/api/invoice-print/drafts',json={'record_key':'x','expected_revision':0,'content':draft()}).status_code == 401
    assert client.post('/api/invoice-print/backup').status_code == 401


def test_draft_pagination(ctx):
    client, session, actor, backup = ctx
    for i in range(51):
        session.add(InvoicePrintRevision(kind='draft', record_key=f'p{i}', revision=1, content=draft(), created_by_id=actor.id))
    session.commit()
    first = client.get('/api/invoice-print/drafts').json();second=client.get('/api/invoice-print/drafts?offset=50').json()
    assert len(first['items']) == 50 and first['has_more']
    assert len(second['items']) == 1 and not second['has_more']
