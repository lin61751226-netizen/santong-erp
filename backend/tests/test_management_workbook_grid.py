from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import AdminAuditLog, Employee, ManagedDocument, Role
from app.routes import management_workbook_grid as routes
from app.services.management_workbook_grid import WorkbookGridError, prepare_changes, read_month_grid


def workbook_bytes():
    book = openpyxl.Workbook()
    other = book.active
    other.title = '保留原表'
    other['B2'] = '=1+2'
    finance = book.create_sheet('115年10月_收支明細表')
    finance['A4'] = '2026-10-01'
    finance['D4'] = '舊摘要'
    finance['H4'] = 100
    finance['J4'] = '=H4-I4'
    finance['A5'] = None
    finance['D5'] = None
    finance['H5'] = None
    finance['J5'] = '=H5-I5'
    payroll = book.create_sheet('115年10月_薪資表')
    payroll['C6'] = '林員工'
    payroll['F6'] = 1000
    payroll['Q6'] = '=SUM(F6:P6)'
    roster = book.create_sheet('115年10月_排休表')
    roster['B6'] = '林員工'
    roster['E6'] = '休'
    result = BytesIO()
    book.save(result)
    book.close()
    return result.getvalue()


def test_read_and_patch_only_selected_cells():
    content = workbook_bytes()
    assert read_month_grid(content, '2026-10', 'finance')['rows'][0]['values']['summary'] == '舊摘要'
    assert read_month_grid(content, '2026-10', 'payroll')['rows'][0]['name'] == '林員工'
    assert read_month_grid(content, '2026-10', 'roster')['rows'][0]['values']['1'] == '休'
    changes, revised = prepare_changes(content, '2026-10', 'finance', [
        {'row_number': 4, 'values': {'summary': '新摘要', 'income': 250}},
    ])
    assert changes[0]['before'] == {'summary': '舊摘要', 'income': 100}
    with ZipFile(BytesIO(content)) as source, ZipFile(BytesIO(revised)) as result:
        changed = [name for name in source.namelist() if source.read(name) != result.read(name)]
        assert changed == ['xl/worksheets/sheet2.xml']
    book = openpyxl.load_workbook(BytesIO(revised))
    assert book['115年10月_收支明細表']['D4'].value == '新摘要'
    assert book['115年10月_收支明細表']['H4'].value == 250
    assert book['115年10月_收支明細表']['J4'].value == '=H4-I4'
    assert book['保留原表']['B2'].value == '=1+2'
    book.close()


def test_rejects_formula_cross_month_and_invalid_finance():
    content = workbook_bytes()
    for edit in (
        {'row_number': 4, 'values': {'net': 1}},
        {'row_number': 4, 'values': {'summary': '=SUM(A1:A2)'}},
        {'row_number': 4, 'values': {'expense': 20}},
        {'row_number': 5, 'values': {'summary': '無日期'}},
        {'row_number': 4, 'values': {'date': '2026-11-01'}},
    ):
        with pytest.raises(WorkbookGridError):
            prepare_changes(content, '2026-10', 'finance', [edit])


@pytest.fixture
def ctx():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    actor = Employee(employee_code='ADMIN001', name='測試管理員', role=Role.admin, bind_token='admin')
    document = ManagedDocument(category='通訊錄與年度管理', title='全年管理',
        original_file_name='三通全年管理.xlsx', stored_file_name='三通全年管理.xlsx',
        drive_file_id='source-1', drive_folder_id='folder', drive_url='https://drive.example/source',
        uploaded_by_id=1)
    session.add_all([actor, document])
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_actor] = lambda: actor
    upload = AsyncMock(return_value=SimpleNamespace(file_name='new.xlsx', file_id='new-1', folder_id='folder', file_url='https://drive.example/new'))
    download = AsyncMock(return_value=workbook_bytes())
    backup = AsyncMock(return_value={'status': 'saved'})
    with patch.object(routes.drive, 'download_file_bytes', download), \
         patch.object(routes.drive, 'upload_management_document', upload), \
         patch.object(routes.drive, 'backup_database', backup):
        yield SimpleNamespace(client=TestClient(app), session=session, actor=actor,
                              document=document, upload=upload, backup=backup, download=download)
    app.dependency_overrides.clear()
    session.close()
    engine.dispose()


def test_api_preview_save_version_and_audit(ctx):
    response = ctx.client.get('/api/management-workbook-grid', params={
        'document_id': ctx.document.id, 'month': '2026-10', 'kind': 'finance'})
    assert response.status_code == 200, response.text
    data = response.json()
    payload = {'document_id': ctx.document.id, 'month': '2026-10', 'kind': 'finance',
        'expected_sha256': data['sha256'], 'rows': [{'row_number': 4, 'values': {'summary': '新摘要'}}]}
    preview = ctx.client.post('/api/management-workbook-grid', json=payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()['changes'][0]['after']['summary'] == '新摘要'
    ctx.upload.assert_not_called()
    assert ctx.client.post('/api/management-workbook-grid', json={**payload, 'preview_only': False}).status_code == 409
    saved = ctx.client.post('/api/management-workbook-grid', json={**payload, 'preview_only': False, 'confirmed': True})
    assert saved.status_code == 200, saved.text
    assert saved.json()['source_document_id'] == ctx.document.id
    assert saved.json()['document_id'] != ctx.document.id
    assert ctx.session.get(ManagedDocument, ctx.document.id).drive_file_id == 'source-1'
    assert ctx.session.exec(select(AdminAuditLog).where(AdminAuditLog.action == 'management_workbook_grid_edit')).one()
    ctx.backup.assert_awaited_once()


def test_api_rejects_stale_hash_and_wrong_category(ctx):
    payload = {'document_id': ctx.document.id, 'month': '2026-10', 'kind': 'finance',
        'expected_sha256': '0' * 64, 'rows': [{'row_number': 4, 'values': {'summary': '新摘要'}}]}
    assert ctx.client.post('/api/management-workbook-grid', json=payload).status_code == 409
    ctx.document.category = '合約'
    ctx.session.add(ctx.document)
    ctx.session.commit()
    assert ctx.client.post('/api/management-workbook-grid', json=payload).status_code == 404
    ctx.upload.assert_not_called()
