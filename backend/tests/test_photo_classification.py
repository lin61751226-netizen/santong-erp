from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import AdminAuditLog, Employee, GroupTextLog, PhotoUploadLog, Role, Worksite
from app.services.google_drive import GoogleDriveWorklogService, google_drive_worklog_service as drive, DriveUploadResult
from app.services.line import _handle_image_message, _save_group_text_log
from app.services.line_platform import line_platform_service as lp
from app.services.photo_classification import (
    parse_caption, is_mixed_photo_group, choose_caption, photo_moment,
    classification_plan, apply_classification,
    reconcile_pending_classification,
)

NOW = datetime(2026, 10, 19, 14, 15, 13)
TODAY = date(2026, 10, 21)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def label(id=1, text="76-2026/10/19", stamp=NOW-timedelta(seconds=26), user="u", group="g"):
    return GroupTextLog(id=id, source_type="group", source_id=group, line_user_id=user, content=text, sent_at=stamp)


def photo(id=1, stamp=NOW, **kwargs):
    return PhotoUploadLog(id=id, line_user_id="u", source_message_id=f"m{id}",
        file_name=f"小戴_{(stamp+timedelta(hours=8)).strftime('%Y%m%d_%H%M%S')}.jpg",
        drive_file_id=f"f{id}", drive_folder_id="old", drive_url=f"https://drive.google.com/file/d/f{id}/view",
        uploaded_at=stamp+timedelta(seconds=5), **kwargs)


@pytest.fixture
def ctx():
    engine = create_engine("sqlite://", connect_args={"check_same_thread":False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        actor=Employee(employee_code="A",name="管理員",role=Role.admin,bind_token="keep",password_hash="same",line_user_id="admin")
        sites=[Worksite(code="47",name="善捷47"), Worksite(code="76",name="金駿76")]
        s.add_all([actor,*sites]);s.commit()
        app.dependency_overrides[get_session]=lambda:s
        app.dependency_overrides[get_current_actor]=lambda:actor
        with patch("app.services.photo_classification.local_today",return_value=TODAY), \
             patch.object(lp,"get_group_display_name",AsyncMock(return_value="47＆76工地相片")), \
             patch.object(drive,"backup_database",AsyncMock(return_value={"status":"saved"})) as backup, \
             patch.object(drive,"move_classified_photo",AsyncMock(return_value="target")) as move:
            yield {"s":s,"actor":actor,"sites":sites,"backup":backup,"move":move,"client":TestClient(app)}
        app.dependency_overrides.clear()
    engine.dispose()


@pytest.mark.parametrize("text,code,day",[("47-2026/10/19","47",date(2026,10,19)),("76-2026/10/19","76",date(2026,10,19)),("４７－２０２６／１０／１９","47",date(2026,10,19))])
def test_caption_exact_format(text,code,day):
    with patch("app.services.photo_classification.local_today",return_value=TODAY):
        assert parse_caption(text)==(code,day)


@pytest.mark.parametrize("text",["47與76工地", "47-2026/2/30", "47-2027/1/1", "47-2026/10/19、76-2026/10/19", "47-115/10/19"])
def test_invalid_or_ambiguous_not_guessed(text):
    assert parse_caption(text) is None


def test_same_sender_boundaries_casual_text_and_following_caption():
    a=label(); b=label(2,"47-2026/10/19",NOW+timedelta(seconds=17))
    assert choose_caption([a,b],user_id="u",moment=NOW,allow_following=True).id==1
    assert choose_caption([a,b,label(3,"何時？",NOW+timedelta(seconds=18))],user_id="u",moment=NOW+timedelta(seconds=20)).id==2
    assert choose_caption([a,b],user_id="other",moment=NOW) is None
    assert choose_caption([a],user_id="u",moment=NOW+timedelta(hours=3)) is None
    assert choose_caption([b],user_id="u",moment=NOW,allow_following=True).id==2
    assert choose_caption([b],user_id="u",moment=NOW) is None
    assert choose_caption([label(9,"47-2026/10/99",NOW)],user_id="u",moment=NOW) is None
    assert choose_caption([a,label(8,"47-2026/10/19",a.sent_at)],user_id="u",moment=NOW) is None


def test_group_name_and_legacy_timestamp_safety():
    assert is_mixed_photo_group("47 ＆ 76 工地相片")
    assert not is_mixed_photo_group("其他47群組")
    assert photo_moment(photo())==NOW
    assert photo_moment(photo(import_key="album")) is None
    p=photo();p.uploaded_at+=timedelta(days=1)
    assert photo_moment(p) is None


def plan(ctx,legacy=True):
    return classification_plan(ctx["s"],group_id="g",date_from=date(2026,10,16),date_to=TODAY,allow_legacy=legacy)


def test_preview_read_only_split_by_caption_and_keep_unknown(ctx):
    s=ctx["s"];s.add_all([label(),label(2,"47-2026/10/19",NOW+timedelta(seconds=17)),photo(),
        photo(2,NOW+timedelta(seconds=20)),photo(3,source_group_id="other")]);s.commit()
    p=plan(ctx)
    assert [(x["site_name"],x["work_date"]) for x in p["rows"]]==[("金駿76","2026-10-19"),("善捷47","2026-10-19")]
    assert p["legacy_count"]==2 and plan(ctx,False)["count"]==0
    assert all(x.site_id is None for x in s.exec(select(PhotoUploadLog)).all())
    assert not s.exec(select(AdminAuditLog)).all()
    ctx["move"].assert_not_awaited()


@pytest.mark.anyio
async def test_apply_moves_original_and_preserves_identity_link_time_and_audits(ctx):
    s=ctx["s"];s.add_all([label(),photo()]);s.commit()
    result=await apply_classification(s,plan(ctx),actor=ctx["actor"])
    assert result["saved"]==1
    p=s.get(PhotoUploadLog,1)
    assert (p.drive_file_id,p.drive_url,p.uploaded_at)==("f1","https://drive.google.com/file/d/f1/view",NOW+timedelta(seconds=5))
    assert (p.work_date,p.source_group_id,p.classification_text_id,p.drive_folder_id)==(date(2026,10,19),"g",1,"target")
    assert "2026/10/19" in s.exec(select(AdminAuditLog)).one().summary
    assert (ctx["actor"].password_hash,ctx["actor"].line_user_id)==("same","admin")
    assert plan(ctx)["count"]==0


@pytest.mark.anyio
async def test_failure_then_retry_does_not_lose_record(ctx):
    s=ctx["s"];s.add_all([label(),photo()]);s.commit()
    ctx["move"].side_effect=RuntimeError("Drive失敗")
    assert (await apply_classification(s,plan(ctx),actor=ctx["actor"]))["saved"]==0
    assert s.get(PhotoUploadLog,1).site_id is None
    ctx["move"].side_effect=None
    assert (await apply_classification(s,plan(ctx),actor=ctx["actor"]))["saved"]==1


def test_authorization_digest_and_backup_gate(ctx):
    s=ctx["s"];s.add_all([label(),photo()]);s.commit()
    data={"date_from":"2026-10-16","date_to":"2026-10-21","allow_legacy":True}
    c=ctx["client"];p=c.post('/api/photo-uploads/classify/preview',json=data).json()
    assert p["count"]==1
    assert c.post('/api/photo-uploads/classify/apply',json={**data,"digest":"bad"}).status_code==409
    ctx["backup"].return_value={"status":"failed"}
    assert c.post('/api/photo-uploads/classify/apply',json={**data,"digest":p["digest"]}).status_code==503
    ctx["move"].assert_not_awaited()
    ctx["actor"].role=Role.employee
    assert c.post('/api/photo-uploads/classify/preview',json=data).status_code==403
    app.dependency_overrides.pop(get_current_actor)
    assert c.post('/api/photo-uploads/classify/preview',json=data).status_code==401


def test_apply_reports_post_backup_failure_honestly(ctx):
    ctx["s"].add_all([label(),photo()]);ctx["s"].commit()
    data={"date_from":"2026-10-16","date_to":"2026-10-21","allow_legacy":True}
    c=ctx["client"];p=c.post('/api/photo-uploads/classify/preview',json=data).json()
    ctx["backup"].side_effect=[{"status":"saved"},{"status":"failed"}]
    r=c.post('/api/photo-uploads/classify/apply',json={**data,"digest":p["digest"]}).json()
    assert r["saved"]==1 and r["backup_status"]=="failed"


@pytest.mark.anyio
async def test_caption_drives_future_unbound_photo_date_not_upload_day(ctx):
    s=ctx["s"];s.add(label(text="47-2026/10/18"));s.commit()
    upload=DriveUploadResult("f","image.jpg","url","parent","path","image/jpeg")
    event={"timestamp":int(NOW.replace(tzinfo=timezone.utc).timestamp()*1000),"source":{"type":"group","groupId":"g","userId":"u"}}
    with patch.object(lp,"get_source_display_name",AsyncMock(return_value="小戴")),patch.object(drive,"upload_line_photo",AsyncMock(return_value=upload)) as send:
        await _handle_image_message(s,event=event,message={"id":"new"},reply_token="r",line_user_id="u",employee=None)
    assert send.await_args.kwargs["work_date"]==date(2026,10,18)
    assert send.await_args.kwargs["site_name"]=="善捷47"
    p=s.exec(select(PhotoUploadLog)).one()
    assert p.work_date==date(2026,10,18) and p.source_group_id=="g" and p.classification_text_id==1


@pytest.mark.anyio
async def test_caption_after_photo_reconciles_only_exact_group(ctx):
    s=ctx["s"];s.add_all([photo(source_group_id="g",source_event_at=NOW),photo(2,source_group_id="other",source_event_at=NOW)]);s.commit()
    stamp=NOW+timedelta(minutes=2)
    event={"timestamp":int(stamp.replace(tzinfo=timezone.utc).timestamp()*1000),"message":{"id":"caption"}}
    with patch('app.services.line.local_today',return_value=date(2026,10,19)):
        await _save_group_text_log(s,event=event,source={"type":"group","groupId":"g"},line_user_id="u",employee=None,content="47-2026/10/18")
    assert s.get(PhotoUploadLog,1).work_date==date(2026,10,18)
    assert s.get(PhotoUploadLog,2).site_id is None


@pytest.mark.anyio
async def test_drive_move_recovers_timeout_and_rejects_foreign_root():
    service=GoogleDriveWorklogService();client=Mock();api=client.files.return_value
    def get(fileId,**kwargs):
        data={"f":{"id":"f","parents":["day"]},"day":{"name":"2026-10-19","parents":["month"]},
              "month":{"name":"2026-10","parents":["site"]},"site":{"name":"金駿76","parents":["root"]}}
        return Mock(execute=Mock(return_value=data[fileId]))
    api.get.side_effect=get
    from app.core.config import settings
    with patch.object(service,"_build_client",return_value=client),patch.object(settings,"google_drive_worklog_folder_id","root"):
        assert await service.move_classified_photo(file_id="f",expected_parent="old",original_day=date(2026,10,20),site_name="金駿76",work_date=date(2026,10,19))=="day"
        api.update.assert_not_called();api.create.assert_not_called()
        with pytest.raises(Exception,match="原未分類"):
            await service.move_classified_photo(file_id="f",expected_parent="old",original_day=date(2026,10,20),site_name="善捷47",work_date=date(2026,10,19))


@pytest.mark.anyio
async def test_periodic_recovery_does_not_touch_legacy_or_other_group(ctx):
    s=ctx["s"];s.add_all([label(),photo(source_group_id="g",source_group="47&76工地照片",source_event_at=NOW),photo(2)]);s.commit()
    r=await reconcile_pending_classification(s)
    assert r["saved"]==1 and r["backup_status"]=="saved"
    assert s.get(PhotoUploadLog,2).site_id is None
    assert (await reconcile_pending_classification(s))["saved"]==0


@pytest.mark.anyio
async def test_recovery_backup_failure_never_moves_original(ctx):
    s=ctx["s"];s.add_all([label(),photo(source_group_id="g",source_group="47&76工地照片",source_event_at=NOW)]);s.commit()
    ctx["backup"].return_value={"status":"failed"}
    assert (await reconcile_pending_classification(s))["saved"]==0
    ctx["move"].assert_not_awaited()


def test_lightweight_migration_preserves_existing_rows():
    from sqlalchemy import inspect
    from app.core.db import _apply_lightweight_migrations
    engine=create_engine("sqlite://")
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE photouploadlog (id INTEGER PRIMARY KEY,employee_id INTEGER,drive_file_id VARCHAR)")
        c.exec_driver_sql("INSERT INTO photouploadlog VALUES (9,NULL,'keep-file')")
    with patch('app.core.db.engine',engine):
        _apply_lightweight_migrations();_apply_lightweight_migrations()
    assert {'source_group_id','source_event_at','classification_text_id'} <= {x['name'] for x in inspect(engine).get_columns('photouploadlog')}
    with engine.connect() as c:
        assert tuple(c.exec_driver_sql("SELECT id,employee_id,drive_file_id FROM photouploadlog").one())==(9,None,'keep-file')
    engine.dispose()


@pytest.mark.anyio
async def test_drive_move_changes_only_parent_not_original_id_name_or_sharing():
    service=GoogleDriveWorklogService();client=Mock();api=client.files.return_value
    data={'f':{'id':'f','parents':['day']},'day':{'name':'2026-10-19','parents':['month']},
          'month':{'name':'2026-10','parents':['site']},'site':{'name':'未分類工地','parents':['root']}}
    api.get.side_effect=lambda fileId,**kw:Mock(execute=Mock(return_value=data[fileId]))
    api.update.return_value.execute.return_value={'id':'f','parents':['target']}
    from app.core.config import settings
    with patch.object(service,'_build_client',return_value=client),patch.object(service,'_find_or_create_photo_folder',return_value='target'),patch.object(settings,'google_drive_worklog_folder_id','root'):
        assert await service.move_classified_photo(file_id='f',expected_parent='day',original_day=date(2026,10,19),site_name='善捷47',work_date=date(2026,10,18))=='target'
    assert api.update.call_args.kwargs=={'fileId':'f','addParents':'target','removeParents':'day','fields':'id,parents','supportsAllDrives':True}
    client.permissions.assert_not_called();api.create.assert_not_called();api.get_media.assert_not_called()
