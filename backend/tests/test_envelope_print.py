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
from app.services.google_drive import google_drive_worklog_service as drive
from app.services.envelope_print import EnvelopeDraft, EnvelopeLayout, render_lines, sender_column_text


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


def valid_draft():
    return {
        'receiver_postcode': '10603',
        'receiver_address': '臺北市大安區金山南路2段55號',
        'receiver_company': '齊裕營造股份有限公司',
        'receiver_name': '王大明',
        'receiver_suffix': '收',
        'sender_postcode': '330',
        'sender_address': '桃園市桃園區永安路385巷6號8樓之3',
        'sender_name': '三通工程行',
        'sender_suffix': '緘',
        'sender_phone': '',
    }


def test_default_layout_is_100x200_with_sender(ctx):
    client, session, actor, backup = ctx
    response = client.get('/api/envelope-print/template')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    content = response.json()['content']
    assert (content['paper_width'], content['paper_height']) == (100, 200)
    keys = set(content['fields'])
    assert keys == {'receiver_postcode', 'receiver_company', 'receiver_address', 'receiver_name',
                    'sender_postcode', 'sender_address', 'sender_name', 'sender_phone'}
    assert content['fields']['receiver_postcode']['x'] > content['fields']['sender_postcode']['x']
    assert session.exec(select(InvoicePrintRevision)).all() == []
    assert client.get('/envelope-print').headers['cache-control'] == 'no-store'


def test_default_sender_is_santong():
    draft = EnvelopeDraft(receiver_address='臺北市測試路1號', receiver_name='王大明')
    assert draft.sender_postcode == '33059'
    assert draft.sender_name == '三通工程行 林金生'
    assert draft.sender_address == '桃園市桃園區永安路385巷6號8樓之3'
    assert draft.sender_phone == '0978-909-078'
    lines = render_lines(draft)
    assert lines['receiver_name'] == '王大明收'
    assert lines['sender_name'] == '三通工程行 林金生緘 電話：0978-909-078'
    assert lines['sender_phone'] == ''
    assert '0978-909-0781' not in lines['sender_name']
    assert lines['sender_address'] == '桃園市桃園區永安路385巷6號8樓之3'


def test_sender_is_two_vertical_columns_and_postcode_stays():
    layout = EnvelopeLayout()
    address = layout.fields['sender_address']
    name = layout.fields['sender_name']
    phone = layout.fields['sender_phone']
    assert address.x > name.x
    assert address.y == name.y == 52
    assert address.height == name.height == 127
    assert address.y + address.height == 179
    assert address.width <= 12 and name.width <= 12
    assert phone.x == name.x and phone.y == name.y
    postcode = layout.fields['sender_postcode']
    assert (postcode.x, postcode.y) == (8.5, 184.5)
    assert name.y + name.height < postcode.y
    assert layout.fields['receiver_postcode'].y == 9.5
    assert layout.fields['receiver_company'].y == 54
    assert layout.fields['receiver_address'].y == 64
    assert layout.fields['receiver_name'].y == 60
    assert layout.fields['receiver_address'].y + layout.fields['receiver_address'].height <= 200
    assert layout.fields['receiver_name'].y + layout.fields['receiver_name'].height <= 200
    no_phone = EnvelopeDraft(receiver_address='臺北市測試路1號', receiver_name='王大明', sender_phone='')
    assert sender_column_text(no_phone) == '三通工程行 林金生緘'
    assert '電話' not in sender_column_text(no_phone)


def test_validate_and_calculate(ctx):
    client, *_ = ctx
    result = client.post('/api/envelope-print/validate', json=valid_draft())
    assert result.status_code == 200
    lines = result.json()['lines']
    assert lines['receiver_company'] == '齊裕營造股份有限公司'
    assert lines['sender_name'] == '三通工程行緘'


@pytest.mark.parametrize('patch', [
    {'receiver_address': ''},
    {'receiver_name': ''},
    {'receiver_postcode': 'abc'},
    {'receiver_postcode': '123456'},
    {'sender_name': ''},
])
def test_bad_draft(ctx, patch):
    client, *_ = ctx
    payload = {**valid_draft(), **patch}
    assert client.post('/api/envelope-print/validate', json=payload).status_code == 422


def test_postcode_dashes_accepted_and_truncated():
    draft = EnvelopeDraft(**{**valid_draft(), 'receiver_postcode': '106-03'})
    assert draft.receiver_postcode == '10603'


def test_layout_revision_conflict_and_audit(ctx):
    client, session, actor, backup = ctx
    content = EnvelopeLayout().model_dump()
    content['offset_x'] = 1
    payload = {'expected_revision': 0, 'content': content}
    first = client.put('/api/envelope-print/template', json=payload)
    assert first.status_code == 200 and first.json()['backup']['status'] == 'saved'
    assert client.put('/api/envelope-print/template', json=payload).status_code == 409
    payload['expected_revision'] = 1; payload['content']['name_font_size'] = 22
    assert client.put('/api/envelope-print/template', json=payload).json()['revision'] == 2
    rows = session.exec(select(InvoicePrintRevision)).all()
    assert len(rows) == 2 and all(r.kind == 'envelope_layout' for r in rows)
    assert len(session.exec(select(AdminAuditLog)).all()) == 2


@pytest.mark.parametrize('key,value', [
    ('name_font_size', 0), ('font_size', 60), ('postcode_spacing', 0),
    ('postcode_gap', 30), ('offset_x', 21), ('paper_width', 79),
])
def test_bad_layout_rejected(ctx, key, value):
    client, session, actor, backup = ctx
    content = EnvelopeLayout().model_dump(); content[key] = value
    assert client.put('/api/envelope-print/template', json={'expected_revision': 0, 'content': content}).status_code == 422
    assert not backup.called


def test_field_outside_paper_rejected(ctx):
    client, *_ = ctx
    content = EnvelopeLayout().model_dump()
    content['fields']['receiver_name']['x'] = 90
    assert client.put('/api/envelope-print/template', json={'expected_revision': 0, 'content': content}).status_code == 422


def test_draft_history_and_pagination(ctx):
    client, session, actor, backup = ctx
    payload = {'record_key': 'env-1', 'expected_revision': 0, 'content': valid_draft()}
    assert client.post('/api/envelope-print/drafts', json=payload).status_code == 200
    payload['expected_revision'] = 1
    payload['content']['receiver_name'] = '李小姐'
    assert client.post('/api/envelope-print/drafts', json=payload).json()['revision'] == 2
    items = client.get('/api/envelope-print/drafts').json()['items']
    assert len(items) == 1 and items[0]['content']['receiver_name'] == '李小姐'
    assert items[0]['record_key'] == 'env-1'
    # Envelope revisions share the table but never appear in invoice draft lists.
    invoice_drafts = client.get('/api/invoice-print/drafts').json()['items']
    assert invoice_drafts == []


def test_sample_layout_and_private(ctx):
    client, session, actor, backup = ctx
    sample = client.get('/api/envelope-print/sample-layout')
    assert sample.status_code == 200
    assert sample.json()['fields']['receiver_postcode']['x'] == 52
    actor.role = Role.employee
    assert client.get('/envelope-print').status_code == 403
    assert client.get('/api/envelope-print/template').status_code == 403
    app.dependency_overrides.pop(get_current_actor)
    assert client.get('/api/envelope-print/template').status_code == 401
    assert client.post('/api/envelope-print/validate', json=valid_draft()).status_code == 401
    assert client.post('/api/envelope-print/pdf', json={'layout': EnvelopeLayout().model_dump(), 'calibration': True}).status_code == 401


def _intersects(a, b, slack=0.05):
    return (a['x'] < b['x'] + b['w'] - slack and b['x'] < a['x'] + a['w'] - slack
            and a['y'] < b['y'] + b['h'] - slack and b['y'] < a['y'] + a['h'] - slack)


def test_envelope_pdf_is_one_page_and_glyphs_do_not_overlap(ctx):
    from app.services.envelope_pdf import layout_boxes, render_envelope_pdf, vertical_font_size
    from tests.test_invoice_print import _media_box, _page_count, _pdf_text
    client, session, actor, backup = ctx
    before = len(session.exec(select(InvoicePrintRevision)).all())
    draft = {
        'receiver_postcode': '10603',
        'receiver_address': '臺北市大安區金山南路2段55號',
        'receiver_company': '齊裕營造',
        'receiver_name': '王大明',
        'receiver_suffix': '收',
        'sender_postcode': '33059',
        'sender_address': '桃園市桃園區永安路385巷6號8樓之3',
        'sender_name': '三通工程行 林金生',
        'sender_suffix': '緘',
        'sender_phone': '0978-909-078',
    }
    layout = EnvelopeLayout()
    response = client.post('/api/envelope-print/pdf', json={'layout': layout.model_dump(), 'draft': draft})
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/pdf'
    assert response.headers['cache-control'] == 'no-store'
    body = response.content
    assert body.startswith(b'%PDF') and _page_count(body) == 1
    width, height = _media_box(body)
    assert abs(width - 100 * 72 / 25.4) < 0.2
    assert abs(height - 200 * 72 / 25.4) < 0.2
    text = _pdf_text(body)
    assert '王大明收' in text
    assert '桃園市桃園區永安路385巷6號8樓之3' in text
    assert '0978-909-078' in text
    assert '33059' in text
    assert '10603' in text
    assert '保存定位' not in text
    assert '白紙校正' not in text
    assert b'DCTDecode' not in body and b'UMing' in body
    assert len(session.exec(select(InvoicePrintRevision)).all()) == before and not backup.called

    content = EnvelopeDraft(**draft)
    boxes = layout_boxes(layout, content)
    for index, left in enumerate(boxes):
        for right in boxes[index + 1:]:
            assert not _intersects(left, right), (left, right)
    # The address column is the right-hand sender column; its digits are upright cells.
    address_text = ''.join(box['text'] for box in boxes if 16 <= box['x'] <= 28 and box['y'] < 170)
    assert address_text == '桃園市桃園區永安路385巷6號8樓之3'
    address_boxes = [box for box in boxes if 16 <= box['x'] <= 28 and box['y'] < 170]
    for earlier, later in zip(address_boxes, address_boxes[1:]):
        assert later['y'] >= earlier['y'] + earlier['h'] - 0.01
    phone = [box for box in boxes if box['sideways']]
    assert ''.join(box['text'] for box in phone) == '0978-909-078'
    # CSS rotate(-90deg) puts the start of the string at the bottom of the run.
    assert phone[0]['y'] == max(box['y'] for box in phone)
    assert phone[0]['y'] > address_boxes[-1]['y']
    assert max(box['y'] + box['h'] for box in phone) < layout.fields['sender_postcode'].y
    assert vertical_font_size('桃園市桃園區永安路385巷6號8樓之3', 50, 10) == 6.4

    blank = client.post('/api/envelope-print/pdf', json={'layout': layout.model_dump(), 'calibration': True})
    assert blank.status_code == 200 and _page_count(blank.content) == 1
    blank_text = _pdf_text(blank.content)
    assert '白紙校正' in blank_text and '貼郵票處' in blank_text
    assert '王大明' not in blank_text and '桃園市' not in blank_text
    assert client.post('/api/envelope-print/pdf', json={'layout': layout.model_dump(), 'calibration': False}).status_code == 422
    direct = render_envelope_pdf(layout, content)
    assert _page_count(direct) == 1


def _rect(x, y, w, h):
    return {'x': x, 'y': y, 'w': w, 'h': h}


def test_photo_columns_miss_stamp_frame_and_land_in_sender_boxes():
    """Digits sit in the six preprinted squares. Columns clear the stamp and the red frame."""
    from app.services.envelope_pdf import layout_boxes
    from app.services.envelope_print import SENDER_POSTCODE_BOXES, STAMP_GUIDE
    layout = EnvelopeLayout()
    draft = EnvelopeDraft(
        receiver_postcode='31046',
        receiver_address='台北市中山區樂群二路267號9樓',
        receiver_company='齊裕營造股份有限公司',
        receiver_name='採發－黃曼婷#2925',
        receiver_suffix='收',
        sender_postcode='33059',
        sender_address='桃園市桃園區永安路385巷6號8樓之3',
        sender_name='三通工程行 林金生',
        sender_suffix='緘',
        sender_phone='810-606-8260',
    )
    boxes = layout_boxes(layout, draft)
    digits = [box for box in boxes if abs(box['y'] - 184.5) < 0.05 and box['text'].isdigit()]
    assert [box['text'] for box in digits] == list('33059')
    for digit, guide in zip(digits, SENDER_POSTCODE_BOXES[:5]):
        assert abs((digit['x'] + digit['w'] / 2) - (guide[0] + guide[2] / 2)) < 0.15
        assert abs((digit['y'] + digit['h'] / 2) - (guide[1] + guide[3] / 2)) < 0.4
        assert _intersects(digit, _rect(*guide))
    stamp = _rect(*STAMP_GUIDE)
    frame = (35.58, 24.83, 33.57, 152.51)
    borders = [
        _rect(frame[0], frame[1], 0.9, frame[3]),
        _rect(frame[0] + frame[2] - 0.9, frame[1], 0.9, frame[3]),
        _rect(frame[0], frame[1], frame[2], 0.9),
        _rect(frame[0], frame[1] + frame[3] - 0.9, frame[2], 0.9),
    ]
    guides = [_rect(*guide) for guide in SENDER_POSTCODE_BOXES]
    for box in boxes:
        if box['text'].strip() == '':
            continue
        assert not _intersects(box, stamp), box
        for border in borders:
            assert not _intersects(box, border), (box, border)
        if box['y'] < 180:
            for guide in guides:
                assert not _intersects(box, guide), (box, guide)
    sender = [box for box in boxes if box['x'] < 30 and box['y'] >= 52 and box['y'] < 180]
    assert sender
    assert min(box['y'] for box in sender) >= 52
    assert max(box['y'] + box['h'] for box in sender) < SENDER_POSTCODE_BOXES[0][1]
