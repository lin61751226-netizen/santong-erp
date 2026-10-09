from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import date, datetime, time
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.config import settings
from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import (
    AiBillingCheck,
    AiJournalDraft,
    AssignmentMember,
    AttendanceEvent,
    Employee,
    Forklift,
    ForkliftInspection,
    GroupTextLog,
    Role,
    SignSlipRecord,
    WorkAssignment,
    WorkHourImportLog,
    Worksite,
    WorksiteJournalHours,
)
from app.services.ai_ops import AiOpsUnavailable, BillingExplanation, BillingExplanations, JournalNarrative
from app.services.google_drive import google_drive_worklog_service
from app.services.scheduler import prepare_ai_ops_drafts


class TestAiOps:
    def setup_method(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.owner = Employee(employee_code="BOSS001", name="老闆", bind_token="boss", role=Role.owner, line_user_id="U-boss")
        self.admin = Employee(employee_code="ADMIN001", name="林金谷", bind_token="admin", role=Role.admin, line_user_id="U-admin")
        self.clerk = Employee(employee_code="ADMIN002", name="秀蓉", bind_token="clerk", role=Role.admin, line_user_id="U-clerk")
        self.worker = Employee(employee_code="EMP009", name="小咪", bind_token="worker", role=Role.employee)
        self.driver = Employee(employee_code="EMP010", name="林建成", bind_token="driver", role=Role.employee)
        self.site = Worksite(code="47", name="善捷47")
        self.qiyu = Worksite(code="53", name="齊裕53")
        self.forklift = Forklift(forklift_code="2號", model="手排 2.5噸柴油車")
        self.session.add_all([self.owner, self.admin, self.clerk, self.worker, self.driver, self.site, self.qiyu, self.forklift])
        self.session.commit()
        self.session.refresh(self.site)
        self.session.refresh(self.forklift)
        self.session.refresh(self.driver)

        app.dependency_overrides[get_session] = lambda: self.session
        app.dependency_overrides[get_current_actor] = lambda: self.owner
        self.client = TestClient(app)
        self.backup = patch.object(google_drive_worklog_service, "backup_database", new_callable=AsyncMock)
        self.backup_mock = self.backup.start()
        self.backup_mock.return_value = {"status": "saved"}

    def teardown_method(self):
        self.backup.stop()
        app.dependency_overrides.clear()
        self.session.close()
        self.engine.dispose()

    def _seed_day(self, work_date: date = date(2026, 10, 9)) -> GroupTextLog:
        assignment = WorkAssignment(
            work_date=work_date,
            site_id=self.site.id,
            work_item="整理鐵管",
            vehicle="2.5噸",
            start_time=time(8, 0),
            end_time=time(17, 0),
        )
        self.session.add(assignment)
        self.session.commit()
        self.session.refresh(assignment)
        self.session.add(AssignmentMember(assignment_id=assignment.id, employee_id=self.worker.id))
        text = GroupTextLog(
            source_type="group",
            source_id="group-1",
            site_id=self.site.id,
            employee_id=self.worker.id,
            content="協助下玻璃 3 片",
            sent_at=datetime(2026, 10, 9, 1, 0),
        )
        self.session.add(text)
        self.session.add(AttendanceEvent(
            employee_id=self.worker.id,
            site_id=self.site.id,
            event_type="上班打卡",
            happened_at=datetime(2026, 10, 9, 0, 0),
        ))
        self.session.add(AttendanceEvent(
            employee_id=self.worker.id,
            site_id=self.site.id,
            event_type="下班打卡",
            happened_at=datetime(2026, 10, 9, 10, 0),
        ))
        self.session.add(ForkliftInspection(
            forklift_id=self.forklift.id,
            operator_id=self.worker.id,
            site_id=self.site.id,
            inspection_date=work_date,
            all_passed=True,
        ))
        self.session.add(GroupTextLog(
            source_type="group",
            source_id="group-unknown",
            content="不知道是哪個工地",
            sent_at=datetime(2026, 10, 9, 2, 0),
        ))
        self.session.commit()
        self.session.refresh(text)
        return text

    def test_disabled_draft_uses_records_and_does_not_call_model(self):
        text = self._seed_day()
        with patch("app.services.ai_ops.complete_journal_narrative", new_callable=AsyncMock) as model:
            response = self.client.post("/api/ai-ops/journals/drafts", json={
                "work_date": "2026-10-09", "worksite_id": self.site.id,
            })
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["backup_status"] == "saved"
        draft = body["draft"]
        assert draft["status"] == "draft"
        assert draft["ai_status"] == "disabled"
        assert draft["applies_to_pricing"] is False
        assert draft["normal_hours"] == 8
        assert draft["content"]["workers"] == ["小咪"]
        assert "整理鐵管" in " ".join(draft["content"]["work_items"])
        assert text.id in draft["source_refs"]["context"]["group_text"]
        model.assert_not_awaited()
        assert self.session.exec(select(WorksiteJournalHours)).all() == []
        assert self.session.get(GroupTextLog, text.id).content == "協助下玻璃 3 片"

    def test_model_citations_are_filtered_and_hours_stay_on_rules(self):
        text = self._seed_day()
        narrative = JournalNarrative(
            work_summary="現場整理鐵管，另有 777 個不明數字。",
            work_items=["整理鐵管"],
            quantities=["玻璃 3 片"],
            issues=[],
            cited_group_text_ids=[text.id, 99999],
        )
        with (
            patch.object(settings, "ai_ops_enabled", True),
            patch("app.services.ai_ops.complete_journal_narrative", new_callable=AsyncMock) as model,
        ):
            model.return_value = narrative
            response = self.client.post("/api/ai-ops/journals/drafts", json={
                "work_date": "2026-10-09", "worksite_id": self.site.id,
            })
        assert response.status_code == 200, response.text
        draft = response.json()["draft"]
        assert draft["ai_status"] == "drafted"
        assert draft["model_name"] == settings.openai_model
        assert draft["normal_hours"] == 8
        assert draft["source_refs"]["cited"]["group_text"] == [text.id]
        assert any("數字" in flag for flag in draft["content"]["review_flags"])
        assert "林建成" not in draft["content"]["workers"]

    def test_model_failure_falls_back_without_blocking_manual_journal(self):
        self._seed_day()
        with (
            patch.object(settings, "ai_ops_enabled", True),
            patch("app.services.ai_ops.complete_journal_narrative", new_callable=AsyncMock) as model,
        ):
            model.side_effect = AiOpsUnavailable("TimeoutError")
            drafted = self.client.post("/api/ai-ops/journals/drafts", json={
                "work_date": "2026-10-09", "worksite_id": self.site.id,
            })
        assert drafted.status_code == 200, drafted.text
        assert drafted.json()["draft"]["ai_status"] == "failed"
        listed = self.client.get("/api/worksite-journals?target_date=2026-10-09")
        assert listed.status_code == 200
        assert listed.json()["sites"][0]["site_name"] == "善捷47"

    def test_approve_does_not_change_sources_or_overwrite_approved_draft(self):
        self._seed_day()
        created = self.client.post("/api/ai-ops/journals/drafts", json={
            "work_date": "2026-10-09", "worksite_id": self.site.id,
        }).json()["draft"]
        approved = self.client.post(f"/api/ai-ops/journals/drafts/{created['id']}/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["draft"]["status"] == "approved"
        assert self.session.exec(select(WorksiteJournalHours)).all() == []
        again = self.client.post("/api/ai-ops/journals/drafts", json={
            "work_date": "2026-10-09", "worksite_id": self.site.id,
        })
        assert again.status_code == 409
        assert len(self.session.exec(select(AiJournalDraft)).all()) == 1

    def test_sign_slip_flags_uncertainty_and_confirm_creates_without_guessing(self):
        self._seed_day()
        drafted = self.client.post("/api/ai-ops/sign-slips/drafts", json={
            "work_date": "2026-10-09", "worksite_id": self.site.id,
        })
        assert drafted.status_code == 200, drafted.text
        draft = drafted.json()["draft"]
        assert draft["normal_hours"] == 8
        assert draft["amount"] is None
        assert draft["customer_name"] in (None, "")
        codes = {item["code"] for item in draft["uncertainties"]}
        assert "missing_driver" in codes
        assert "unknown_customer" in codes
        assert "missing_amount" in codes
        assert "hours_inconsistent_with_clock" in codes
        assert any("沒有改成打卡時數" in item["message"] for item in draft["uncertainties"])
        blocked = self.client.post(f"/api/ai-ops/sign-slips/drafts/{draft['id']}/confirm", json={
            "slip_no": "0003001", "acknowledge_uncertainties": True,
        })
        assert blocked.status_code == 409
        assert self.session.exec(select(SignSlipRecord)).all() == []

        draft["customer_name"] = "善捷營造"
        draft["driver_names"] = "林建成"
        draft["amount"] = 6000
        saved = self.client.put(f"/api/ai-ops/sign-slips/drafts/{draft['id']}", json=self._slip_body(draft))
        assert saved.status_code == 200, saved.text
        assert saved.json()["draft"]["amount"] == 6000
        still = self.client.post(f"/api/ai-ops/sign-slips/drafts/{draft['id']}/confirm", json={
            "slip_no": "0003001", "acknowledge_uncertainties": False,
        })
        assert still.status_code == 409
        confirmed = self.client.post(f"/api/ai-ops/sign-slips/drafts/{draft['id']}/confirm", json={
            "slip_no": "0003001", "acknowledge_uncertainties": True,
        })
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["backup_status"] == "saved"
        stored = self.session.exec(select(SignSlipRecord)).one()
        assert stored.slip_no == "0003001"
        assert stored.amount == 6000
        assert stored.normal_hours == 8
        assert stored.driver_names == "林建成"

    def test_qiyu_customer_comes_from_rule(self):
        assignment = WorkAssignment(work_date=date(2026, 10, 9), site_id=self.qiyu.id, work_item="吊掛", vehicle="3.0噸")
        self.session.add(assignment)
        self.session.commit()
        self.session.refresh(assignment)
        self.session.add(AssignmentMember(assignment_id=assignment.id, employee_id=self.driver.id))
        self.session.commit()
        response = self.client.post("/api/ai-ops/sign-slips/drafts", json={
            "work_date": "2026-10-09", "worksite_id": self.qiyu.id,
        })
        assert response.status_code == 200, response.text
        draft = response.json()["draft"]
        assert draft["customer_name"] == "齊裕營造"
        assert draft["driver_names"] == "林建成"
        assert "unknown_customer" not in {item["code"] for item in draft["uncertainties"]}
        assert "missing_driver" not in {item["code"] for item in draft["uncertainties"]}

    def test_confirm_does_not_modify_existing_sign_slip(self):
        self._seed_day()
        existing = SignSlipRecord(
            slip_no="0002999", slip_date=date(2026, 10, 9), worksite_id=self.site.id,
            site_code="47", customer_name="既有客戶", amount=18000, normal_hours=8, total_hours=8,
            driver_names="朱勝忠",
        )
        self.session.add(existing)
        self.session.commit()
        draft = self.client.post("/api/ai-ops/sign-slips/drafts", json={
            "work_date": "2026-10-09", "worksite_id": self.site.id,
        }).json()["draft"]
        draft["customer_name"] = "善捷營造"
        draft["driver_names"] = "林建成"
        draft["amount"] = 1
        self.client.put(f"/api/ai-ops/sign-slips/drafts/{draft['id']}", json=self._slip_body(draft))
        response = self.client.post(f"/api/ai-ops/sign-slips/drafts/{draft['id']}/confirm", json={
            "slip_no": "0003002", "acknowledge_uncertainties": True,
        })
        assert response.status_code == 409
        stored = self.session.exec(select(SignSlipRecord)).one()
        assert stored.slip_no == "0002999"
        assert stored.amount == 18000

    def test_billing_report_lists_differences_without_changing_amounts(self):
        self.session.add(WorksiteJournalHours(
            work_date=date(2026, 9, 4), worksite_id=self.site.id, normal_hours=16, overtime_hours=0, support_hours=0,
        ))
        self.session.add(SignSlipRecord(
            slip_no="0002766", slip_date=date(2026, 9, 4), worksite_id=self.site.id, site_code="47",
            normal_hours=16, overtime_hours=1, total_hours=16, amount=37000, driver_names="林建成",
            start_time="08:00", end_time="17:00",
        ))
        self.session.add(WorkHourImportLog(
            managed_document_id=1, worksite_id=self.site.id, work_date=date(2026, 9, 4),
            worksheet_name="11509-47", target_label="47", target_row=10,
            normal_hours=8, overtime_hours=0, support_hours=0,
            drive_file_id="file", drive_url="https://example.invalid/file",
        ))
        self.session.commit()
        response = self.client.post("/api/ai-ops/billing/checks", json={"month": "2026-09"})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        check = response.json()["check"]
        assert check["changes_amounts"] is False
        codes = {row["code"] for row in check["report"]["rows"]}
        assert "slip_total_mismatch" in codes
        assert "journal_vs_billed" in codes
        assert "slip_vs_billed" in codes
        slip = self.session.exec(select(SignSlipRecord)).one()
        imported = self.session.exec(select(WorkHourImportLog)).one()
        assert slip.amount == 37000
        assert slip.total_hours == 16
        assert imported.normal_hours == 8
        assert response.json()["backup_status"] == "saved"

    def test_billing_explanation_with_unknown_numbers_is_discarded(self):
        self.session.add(WorksiteJournalHours(
            work_date=date(2026, 9, 4), worksite_id=self.site.id, normal_hours=16,
        ))
        self.session.add(WorkHourImportLog(
            managed_document_id=1, worksite_id=self.site.id, work_date=date(2026, 9, 4),
            worksheet_name="11509-47", target_label="47", target_row=10,
            normal_hours=8, drive_file_id="file", drive_url="https://example.invalid/file",
        ))
        self.session.commit()
        explanations = BillingExplanations(items=[BillingExplanation(
            index=0, explanation="應改成 12345 元。", suggested_fix="自動寫入 12345。",
        )])
        with (
            patch.object(settings, "ai_ops_enabled", True),
            patch("app.services.ai_ops.complete_billing_explanations", new_callable=AsyncMock) as model,
        ):
            model.return_value = explanations
            response = self.client.post("/api/ai-ops/billing/checks", json={"month": "2026-09"})
        assert response.status_code == 200, response.text
        row = response.json()["check"]["report"]["rows"][0]
        assert row["explanation_source"] == "template"
        assert "12345" not in row["explanation"]
        assert response.json()["check"]["ai_status"] == "failed"
        assert self.session.get(WorkHourImportLog, 1).normal_hours == 8

    def test_permissions_and_private_cache(self):
        app.dependency_overrides[get_current_actor] = lambda: self.worker
        forbidden = self.client.post("/api/ai-ops/journals/drafts", json={
            "work_date": "2026-10-09", "worksite_id": self.site.id,
        })
        assert forbidden.status_code == 403
        app.dependency_overrides.pop(get_current_actor)
        anonymous = self.client.get("/api/ai-ops/status")
        assert anonymous.status_code == 401

    def test_schedule_is_off_by_default_and_notifies_once_when_enabled(self):
        disabled = asyncio.run(prepare_ai_ops_drafts(date(2026, 10, 9)))
        assert disabled["status"] == "disabled"
        assert self.session.exec(select(AiJournalDraft)).all() == []

        self._seed_day()

        @contextmanager
        def test_session_scope():
            yield self.session

        with (
            patch.object(settings, "ai_ops_schedule_enabled", True),
            patch.object(settings, "ai_ops_enabled", False),
            patch("app.services.scheduler.session_scope", test_session_scope),
            patch("app.services.scheduler.google_drive_worklog_service.backup_database", new=self.backup_mock),
            patch("app.services.line.line_service.push_text", new_callable=AsyncMock) as push,
        ):
            push.return_value = (True, "sent")
            first = asyncio.run(prepare_ai_ops_drafts(date(2026, 10, 9)))
            second = asyncio.run(prepare_ai_ops_drafts(date(2026, 10, 9)))
        assert first["status"] == "sent"
        assert first["created"]
        assert "未判定" in first["message"]
        assert "OPENAI" not in first["message"]
        assert second["status"] == "already_sent"
        assert second["created"] == []
        assert len(self.session.exec(select(AiJournalDraft)).all()) == 1
        assert self.session.exec(select(AiBillingCheck)).all() == []

    def _slip_body(self, draft: dict) -> dict:
        return {
            "customer_name": draft["customer_name"],
            "work_content": draft["work_content"] or "",
            "driver_names": draft["driver_names"],
            "normal_hours": draft["normal_hours"],
            "overtime_hours": draft["overtime_hours"],
            "support_hours": draft["support_hours"],
            "start_time": draft["start_time"],
            "end_time": draft["end_time"],
            "amount": draft["amount"],
            "vehicles": draft["vehicles"],
        }
