from fastapi.testclient import TestClient

from app.main import app


def test_public_about_explains_drive_permission_without_login():
    response = TestClient(app).get("/about")

    assert response.status_code == 200
    assert "Google Drive 用途" in response.text
    assert "完整 Drive 存取權" in response.text
    assert 'href="/privacy"' in response.text


def test_public_privacy_explains_data_handling_without_login():
    response = TestClient(app).get("/privacy")

    assert response.status_code == 200
    assert "工作照片" in response.text
    assert "沒有固定自動刪除期限" in response.text
    assert "lin61751226@gmail.com" in response.text
