from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import EditingDraftRevision, Employee, Role, SignSlipRecord, WorkHourImportLog
from app.services.google_drive import PRESERVED_DATABASE_TABLES, google_drive_worklog_service


class TestEditingDrafts:
    def setup_method(self):
        self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session=Session(self.engine)
        self.actor=Employee(employee_code='ADMIN001',name='Admin',role=Role.admin,bind_token='one')
        self.other=Employee(employee_code='ADMIN002',name='Other',role=Role.admin,bind_token='two')
        self.session.add_all([self.actor,self.other]); self.session.commit()
        app.dependency_overrides[get_session]=lambda:self.session
        app.dependency_overrides[get_current_actor]=lambda:self.actor
        self.client=TestClient(app)

    def teardown_method(self):
        app.dependency_overrides.clear(); self.session.close(); self.engine.dispose()

    def save(self,revision=0,**kwargs):
        return self.client.put('/api/editing-drafts/sign-slip',json={
            'expected_revision':revision,'payload':{'fields':{'signSlipContent':'unfinished work'}},**kwargs})

    def test_private_append_only_and_no_business_write(self):
        saved=self.save()
        assert saved.status_code==200,saved.text
        assert saved.json()['backup_status']=='pending'
        assert saved.headers['cache-control']=='no-store'
        app.dependency_overrides[get_current_actor]=lambda:self.other
        assert self.client.get('/api/editing-drafts/sign-slip').json()['active'] is False
        app.dependency_overrides[get_current_actor]=lambda:self.actor
        assert self.client.get('/api/editing-drafts/sign-slip').json()['payload']['fields']['signSlipContent']=='unfinished work'
        assert self.save(1,active=False).status_code==200
        assert not self.client.get('/api/editing-drafts/sign-slip').json()['active']
        assert len(self.session.exec(select(EditingDraftRevision)).all())==2
        assert self.session.exec(select(SignSlipRecord)).all()==[]
        assert self.session.exec(select(WorkHourImportLog)).all()==[]

    def test_stale_revision_and_unknown_fields_rejected(self):
        assert self.save().status_code==200
        assert self.save(0).status_code==409
        assert self.save(1,payload={'fields':{'password':'not a draft field'}}).status_code==422
        assert self.save(1,payload={'fields':{},'password':'no'}).status_code==422
        assert self.save(1,payload={'fields':{'signSlipContent':'a'*150001}}).status_code==422
        assert len(self.session.exec(select(EditingDraftRevision)).all())==1

    def test_only_admin_roles_and_kinds(self):
        self.actor.role=Role.employee
        assert self.client.get('/api/editing-drafts/sign-slip').status_code==403
        self.actor.role=Role.admin
        assert self.client.get('/api/editing-drafts/unknown').status_code==422

    def test_grid_round_trip_and_explicit_backup_status(self):
        data={'document_id':7,'year':2026,'month':11,'label':'53','sha256':'a'*64,
              'edits':[['2026-11-01',{'normal_hours':8}]]}
        response=self.client.put('/api/editing-drafts/cost-grid',json={'expected_revision':0,'payload':data})
        assert response.status_code==200,response.text
        assert response.json()['payload']==data
        assert 'editingdraftrevision' in PRESERVED_DATABASE_TABLES
        assert 'costgridamountadjustment' in PRESERVED_DATABASE_TABLES
        with patch.object(google_drive_worklog_service,'backup_database',new_callable=AsyncMock) as backup:
            backup.return_value={'status':'failed'}
            assert self.client.post('/api/editing-drafts/backup/cloud').json()=={'backup_status':'failed'}
