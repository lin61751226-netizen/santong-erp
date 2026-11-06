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
from app.services.invoice_print import (
    FieldSpec, PrintDraft, PrintLayout, calculate, invoice_uppercase_digits,
    invoice_uppercase_twd, uppercase_twd,
)


def test_reference_photo_is_private_and_field_font_size_is_valid(ctx):
    client, _, _, _ = ctx
    photo = client.get('/api/invoice-print/reference-image')
    assert photo.status_code == 200
    assert photo.headers['content-type'] == 'image/jpeg'
    assert photo.headers['cache-control'] == 'private, no-store'
    assert photo.content.startswith(b'\xff\xd8')
    assert FieldSpec(x=1, y=2, width=3, font_size=18).font_size == 18
    with pytest.raises(ValueError):
        FieldSpec(x=1, y=2, width=3, font_size=25)

    app.dependency_overrides.clear()
    assert client.get('/api/invoice-print/reference-image').status_code == 401


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


@pytest.mark.parametrize('value,digits,printed', [
    (0, '零零零零零零零零零', '零億零仟零佰零拾零萬零仟零佰零拾零元'),
    (125475, '零零零壹貳伍肆柒伍', '零億零仟零佰壹拾貳萬伍仟肆佰柒拾伍元'),
    (100000001, '壹零零零零零零零壹', '壹億零仟零佰零拾零萬零仟零佰零拾壹元'),
    (999999999, '玖玖玖玖玖玖玖玖玖', '玖億玖仟玖佰玖拾玖萬玖仟玖佰玖拾玖元'),
])
def test_invoice_uppercase_uses_every_preprinted_slot(value, digits, printed):
    assert ''.join(invoice_uppercase_digits(value)) == digits
    assert invoice_uppercase_twd(value) == printed


@pytest.mark.parametrize('value', [-1, 1.5, True, 1_000_000_000])
def test_invoice_uppercase_rejects_out_of_range(value):
    with pytest.raises(ValueError):
        invoice_uppercase_digits(value)


def test_decimal_rounding_and_roc():
    payload = draft(); payload['items'][0].update(quantity='1.5', unit_price='100.33'); payload['tax'] = 0
    assert calculate(PrintDraft(**payload)) == {'amounts': [150], 'subtotal': 150, 'tax': 0,
        'total': 150, 'uppercase': '零億零仟零佰零拾零萬零仟壹佰伍拾零元',
        'uppercase_digits': list('零零零零零零壹伍零'), 'roc_year': 115}
    payload['items'][0]['unit_price'] = '101'
    assert calculate(PrintDraft(**payload))['total'] == 152


def test_photo_digit_centers_match_measured_squares():
    """八個統編方格與九個大寫單位，中心須落在參考照片量到的位置。"""
    layout = PrintLayout()
    measured_tax = [50.06, 55.96, 61.86, 67.77, 73.67, 79.63, 85.58, 91.49]
    tax_centers = [layout.fields['tax_id'].x + (i + 0.5) * layout.tax_id_spacing for i in range(8)]
    assert all(abs(got - expect) < 0.12 for got, expect in zip(tax_centers, measured_tax))
    measured_upper = [51.79, 61.27, 71.02, 80.61, 90.37, 99.84, 109.49, 118.99, 128.63]
    pitch = layout.fields['uppercase'].width / 9
    upper_centers = [layout.fields['uppercase'].x + (i + 0.5) * pitch for i in range(9)]
    assert all(abs(got - expect) < 0.2 for got, expect in zip(upper_centers, measured_upper))
    assert layout.fields['uppercase'].y == 85.9


def test_default_layout_get_is_read_only(ctx):
    client, session, actor, backup = ctx
    response = client.get('/api/invoice-print/template')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    data = response.json(); assert data['revision'] == 0
    assert data['content']['fields']['buyer']['x'] == 35
    assert data['content']['fields']['buyer']['y'] == 15.5
    assert (data['content']['paper_width'],data['content']['paper_height']) == (195,103)
    assert data['content']['feed_mode'] == 'portrait_cw'
    assert 'address' in data['content']['fields']
    assert 'unit' not in data['content']['fields']
    assert data['content']['fields']['note']['height'] > 0
    assert data['content']['fields']['uppercase']['x'] == 47.0
    assert data['content']['fields']['uppercase']['y'] == 85.9
    assert data['content']['fields']['uppercase']['width'] == 86.5
    assert data['content']['fields']['tax_id']['x'] == 47.1
    assert data['content']['fields']['tax_id']['y'] == 16.4
    assert data['content']['fields']['tax_id']['height'] == 5.5
    assert data['content']['tax_id_spacing'] == 5.92
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


@pytest.mark.parametrize('key,value', [('font','url(https://bad)'),('offset_x',21),('font_size',0),('row_pitch',30),('tax_id_spacing',-1),('offset_y',float('inf')),('feed_mode','sideways')])
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
    assert client.post('/api/invoice-print/pdf', json={'layout': PrintLayout().model_dump(), 'calibration': True}).status_code == 401


def test_draft_pagination(ctx):
    client, session, actor, backup = ctx
    for i in range(51):
        session.add(InvoicePrintRevision(kind='draft', record_key=f'p{i}', revision=1, content=draft(), created_by_id=actor.id))
    session.commit()
    first = client.get('/api/invoice-print/drafts').json();second=client.get('/api/invoice-print/drafts?offset=50').json()
    assert len(first['items']) == 50 and first['has_more']
    assert len(second['items']) == 1 and not second['has_more']


def test_legacy_layout_read_does_not_resize_or_rewrite(ctx):
    client,session,actor,backup=ctx
    # Simulate a real v1 revision: no paper dims, no feed_mode, a 單位 column,
    # and no 買受人地址 row.
    old=PrintLayout().model_dump()
    old.pop('paper_width');old.pop('paper_height');old.pop('feed_mode')
    old.pop('tax_id_spacing');old.pop('row_pitch')
    old['fields']['buyer'].update(x=27,y=22);old['fields']['tax_id'].update(x=30,y=12)
    old['fields']['unit']={'x':55,'y':38,'width':10,'height':0}
    old['fields'].pop('address')
    row=InvoicePrintRevision(kind='layout',record_key='shared',revision=1,content=deepcopy(old),created_by_id=actor.id)
    session.add(row);session.commit()
    result=client.get('/api/invoice-print/template').json()
    assert (result['content']['paper_width'],result['content']['paper_height']) == (190,105)
    assert result['content']['fields']['buyer']['y']==22
    assert result['content']['feed_mode'] == 'portrait_cw'
    assert result['content']['fields']['address']['y'] == 24.5
    assert 'unit' not in result['content']['fields']
    session.refresh(row);assert row.content==old and not backup.called
    assert client.put('/api/invoice-print/template',json={'expected_revision':1,'content':old}).status_code==422
    assert len(session.exec(select(InvoicePrintRevision)).all())==1


def test_sample_is_private_read_only_and_buyer_above_tax_id(ctx):
    client,session,actor,backup=ctx
    sample=client.get('/api/invoice-print/sample-layout')
    assert sample.status_code==200 and sample.headers['cache-control']=='no-store'
    content=sample.json()
    assert (content['paper_width'],content['paper_height'])==(195,103)
    assert content['feed_mode']=='portrait_cw'
    assert content['fields']['buyer']['y'] < content['fields']['tax_id']['y']
    assert content['fields']['address']['y'] > content['fields']['tax_id']['y']
    assert set(content['fields']) == {'buyer','tax_id','address','year','month','day',
        'description','quantity','unit_price','amount','subtotal','tax','total','uppercase','note'}
    assert content['fields']['note']['x'] > content['fields']['amount']['x']
    assert not session.exec(select(InvoicePrintRevision)).all() and not backup.called
    app.dependency_overrides.pop(get_current_actor)
    assert client.get('/api/invoice-print/sample-layout').status_code==401


@pytest.mark.parametrize('patch', [{'paper_width':79},{'paper_height':298},{'paper_height':87},{'paper_width':'NaN'}])
def test_paper_dimensions_validate_with_fields(ctx,patch):
    client,session,actor,backup=ctx
    content={**PrintLayout().model_dump(),**patch}
    assert client.put('/api/invoice-print/template',json={'expected_revision':0,'content':content}).status_code==422
    assert not session.exec(select(InvoicePrintRevision)).all() and not backup.called


def test_explicit_resize_keeps_old_revision(ctx):
    client,session,actor,backup=ctx
    old=PrintLayout().model_dump();old.update(paper_width=190,paper_height=105)
    assert client.put('/api/invoice-print/template',json={'expected_revision':0,'content':old}).status_code==200
    new=PrintLayout().model_dump()
    assert client.put('/api/invoice-print/template',json={'expected_revision':1,'content':new}).json()['revision']==2
    rows=session.exec(select(InvoicePrintRevision).order_by(InvoicePrintRevision.id)).all()
    assert rows[0].content['paper_width']==190 and rows[1].content['paper_width']==195


def _page_count(data: bytes) -> int:
    import re
    return len(re.findall(rb"/Type\s*/Page(?!s)", data))


def _media_box(data: bytes) -> tuple[float, float]:
    import re
    match = re.search(rb"/MediaBox\s*\[\s*([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s*\]", data)
    assert match, data[:200]
    x0, y0, x1, y1 = (float(item) for item in match.groups())
    return x1 - x0, y1 - y0


def _pdf_text(data: bytes) -> str:
    """Read ReportLab page text via ToUnicode bfchar and literal Tj strings."""
    import base64
    import re
    import zlib
    streams = []
    for match in re.finditer(rb"\d+ 0 obj\n<<", data):
        header_end = data.find(b">>", match.end())
        header = data[match.end():header_end]
        length_match = re.search(rb"/Length\s+(\d+)", header)
        if not length_match:
            continue
        stream_at = data.find(b"stream\n", header_end)
        if stream_at < 0 or stream_at - header_end > 8:
            continue
        raw = data[stream_at + 7:stream_at + 7 + int(length_match.group(1))]
        if b"ASCII85Decode" in header:
            raw = base64.a85decode(raw)
        if b"FlateDecode" in header:
            raw = zlib.decompress(raw)
        streams.append(raw)
    mapping = {}
    for stream in streams:
        for src, dst in re.findall(rb"<([0-9A-Fa-f]{2})>\s*<([0-9A-Fa-f]{4})>", stream):
            mapping[int(src, 16)] = int(dst, 16)
    chars = []
    for stream in streams:
        for literal in re.findall(rb"\((?:\\.|[^\\)])*\)\s*Tj", stream):
            body = literal[:literal.rfind(b")")][1:]
            decoded = bytearray()
            index = 0
            while index < len(body):
                if body[index] != 0x5C:
                    decoded.append(body[index])
                    index += 1
                    continue
                nxt = body[index + 1:index + 2]
                if nxt in b"01234567":
                    digits = bytearray()
                    cursor = index + 1
                    while cursor < len(body) and cursor < index + 4 and body[cursor] in b"01234567":
                        digits.append(body[cursor])
                        cursor += 1
                    decoded.append(int(digits, 8))
                    index = cursor
                elif nxt in b"nrtbf\\()":
                    decoded.append({ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}.get(nxt[0], nxt[0]))
                    index += 2
                else:
                    index += 1
            for byte in decoded:
                chars.append(chr(mapping[byte]) if byte in mapping else chr(byte))
    return "".join(chars)


def test_invoice_pdf_is_one_rotated_page_without_chrome(ctx):
    from app.services.invoice_pdf import css_page_point, render_invoice_pdf
    client, session, actor, backup = ctx
    assert css_page_point("portrait_cw", 0, 0, 195, 103) == (103, 0)
    assert css_page_point("portrait_cw", 195, 103, 195, 103) == (0, 195)
    assert css_page_point("portrait_ccw", 0, 0, 195, 103) == (0, 195)
    assert css_page_point("landscape", 10, 4, 195, 103) == (10, 4)
    before = len(session.exec(select(InvoicePrintRevision)).all())
    layout = PrintLayout().model_dump()
    response = client.post("/api/invoice-print/pdf", json={"layout": layout, "draft": draft(), "calibration": False})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    body = response.content
    assert body.startswith(b"%PDF")
    assert _page_count(body) == 1
    width, height = _media_box(body)
    assert abs(width - 103 * 72 / 25.4) < 0.2
    assert abs(height - 195 * 72 / 25.4) < 0.2
    text = _pdf_text(body)
    assert "測試買受人" in text
    assert "12345678" in text
    assert "堆高機作業" in text
    assert "18,900" in text
    assert "保存定位" not in text
    assert "白紙校正" not in text
    assert "列印" not in text
    assert b"/Subtype /Image" not in body
    assert b"DCTDecode" not in body
    assert b"UMing" in body
    assert len(session.exec(select(InvoicePrintRevision)).all()) == before
    assert not backup.called

    blank = client.post("/api/invoice-print/pdf", json={"layout": layout, "calibration": True})
    assert blank.status_code == 200
    blank_pdf = blank.content
    assert _page_count(blank_pdf) == 1
    blank_text = _pdf_text(blank_pdf)
    assert "白紙校正" in blank_text
    assert "測試買受人" not in blank_text
    assert "保存定位" not in blank_text
    assert b"DCTDecode" not in blank_pdf
    assert len(session.exec(select(InvoicePrintRevision)).all()) == before

    ccw = render_invoice_pdf(PrintLayout(feed_mode="portrait_ccw"), PrintDraft(**draft()))
    land = render_invoice_pdf(PrintLayout(feed_mode="landscape"), PrintDraft(**draft()))
    assert _page_count(ccw) == 1 and _page_count(land) == 1
    ccw_box, land_box = _media_box(ccw), _media_box(land)
    assert abs(ccw_box[0] - width) < 0.2 and abs(ccw_box[1] - height) < 0.2
    assert abs(land_box[0] - 195 * 72 / 25.4) < 0.2
    assert abs(land_box[1] - 103 * 72 / 25.4) < 0.2
    assert client.post("/api/invoice-print/pdf", json={"layout": layout, "calibration": False}).status_code == 422
