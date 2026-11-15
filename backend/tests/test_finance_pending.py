from datetime import date
from io import BytesIO
from unittest.mock import AsyncMock, patch

import openpyxl
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.deps import get_current_actor
from app.main import app
from app.models import Employee, FinanceEntry, FinancePendingReview, Role, WorkbookArchive
from app.routes import finance_pending


def source_bytes():
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "115年10月_收支明細表"
    sheet.append([])
    sheet.append([])
    sheet.append(["日期", "類型", "類別", "摘要", "對象", "付款", "憑證", "收入", "支出"])
    sheet.append([date(2026, 10, 1), None, None, "上月結餘", None, None, None, 174002])
    sheet.append([date(2026, 10, 1), "收入", None, "工程款", None, "現金", None, 16000])
    sheet.append([date(2026, 10, 2), "支出", None, "租賃", "甲", "匯款", None, None, 10000])
    sheet.append([date(2026, 10, 2), "支出", None, "租賃", "甲", "匯款", None, None, 10000])
    sheet.append([None, "支出", None, "日期不明", None, None, None, None, 48000])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_stage_classify_and_book_one_by_one():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    actor = Employee(employee_code="ADMIN001", name="管理員", bind_token="admin", role=Role.admin)
    content = source_bytes()
    archive = WorkbookArchive(source_document_id=1, content_sha256="a" * 64, ledger_scope="liuhe",
                              source_role="independent", file_name="六和.xlsx", size_bytes=len(content),
                              sheet_names=["115年10月_收支明細表"], content=content, archived_by_id=1)
    session.add_all([actor, archive])
    session.commit()
    session.refresh(archive)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_actor] = lambda: actor
    try:
        with patch.object(finance_pending.drive, "backup_database", AsyncMock(return_value={"status": "saved"})):
            client = TestClient(app)
            payload = {"archive_id": archive.id, "month": "2026-10"}
            staged = client.post("/api/finance-pending/stage", json=payload)
            assert staged.status_code == 200, staged.text
            assert staged.json()["staged"] == 3
            assert client.post("/api/finance-pending/stage", json=payload).json()["staged"] == 0
            assert session.exec(select(FinanceEntry)).all() == []
            rows = client.get("/api/finance-pending?month=2026-10").json()
            assert [row["source_row"] for row in rows] == [5, 6, 7]
            first_id = rows[0]["id"]
            assert client.post(f"/api/finance-pending/{first_id}/book").status_code == 409
            assert client.patch(f"/api/finance-pending/{first_id}/category", json={"category": "工程款收入"}).status_code == 200
            assert session.exec(select(FinanceEntry)).all() == []
            assert client.post(f"/api/finance-pending/{first_id}/book").status_code == 200
            assert client.post(f"/api/finance-pending/{first_id}/book").status_code == 409
            assert len(session.exec(select(FinanceEntry)).all()) == 1
            for row in rows[1:]:
                assert client.patch(f"/api/finance-pending/{row['id']}/category", json={"category": "租賃費"}).status_code == 200
                assert client.post(f"/api/finance-pending/{row['id']}/book").status_code == 200
            assert len(session.exec(select(FinanceEntry)).all()) == 3
            assert len(session.exec(select(FinancePendingReview).where(FinancePendingReview.status == "booked")).all()) == 3
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()
