# 三通工程自動化管理系統

這個 repo 是 `三通工程自動化管理系統` 的第一版骨架，定位不是單純 LINE Bot，而是以 `LINE 為入口、資料庫為核心、管理後台為控制中心` 的內部自動化系統。

第一版先鎖定六項可實作範圍：

1. LINE 身分綁定與角色權限
2. 後台建立每日工作安排
3. 每日自動推播工作行程
4. 臨時通知與會議記錄發送
5. 員工請假申請與主管核准
6. 個人考勤與工作安排查詢

## 技術架構

- 後端：FastAPI
- 資料模型：SQLModel
- 正式資料庫：PostgreSQL
- 本機快速啟動：SQLite fallback
- 排程：APScheduler
- LINE：Messaging API Webhook + Push Message
- 管理後台：FastAPI 內建模板頁面
- 文件/行事曆：預留 Google Drive / Google Calendar 擴充點

這個版本已經內建：

- 員工、工地、工作安排、通知、請假、考勤、會議資料表
- 角色權限基礎（owner/admin 可登入後台）
- 後台正式登入（統一預設密碼 + 強制改密碼 + 3 次失敗鎖定）
- LINE webhook 驗簽與文字指令處理
- 每日 07:00 工作安排排程推播
- 可操作的管理後台首頁
- Demo 種子資料（8 員工 + 10 工地）
- Google Drive 工作相片自動上傳

## 專案結構

```text
backend/
  app/
    core/
    routes/
    services/
    static/
    templates/
    main.py
  requirements.txt
docker-compose.yml
.env.example
```

## 角色模型

- `owner`：老闆，可查看全部與核准重大事項
- `admin`：行政／管理者，可建立工作安排、通知、會議、審核請假
- `accounting`：會計，保留給後續財務模組
- `site_manager`：工地主任，可處理所屬工地的人員、工作與請假
- `employee`：一般員工，只能看自己資料與提交請假
- `external`：外部人員，僅保留指定通知

## LINE 指令

第一版支援下列文字指令：

- `綁定 ST-1001`
- `我的行程`
- `我的打卡`
- `請假 事假 2026-07-28 2026-07-28 家中有事`
- `已收到`
- `已到場`
- `工作開始`
- `工作完成`
- `異常回報 現場缺料`

## 本機啟動

### 方式一：Python 本機執行

1. 安裝 Python 3.11 以上
2. 建立虛擬環境
3. 安裝套件
4. 複製 `.env.example` 成 `backend/.env`
5. 啟動 API

Windows PowerShell 範例：

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
Copy-Item .env.example backend\.env
uvicorn app.main:app --app-dir backend --reload
```

開啟：

- 管理後台：[http://127.0.0.1:8000/](http://127.0.0.1:8000/)
- OpenAPI 文件：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

### 方式二：Docker Compose

```powershell
docker compose up --build
```

預設會啟動：

- `api`：FastAPI
- `db`：PostgreSQL 16

## 環境變數

主要設定放在 `backend/.env`：

```env
APP_NAME=三通工程自動化管理系統
ENVIRONMENT=development
DATABASE_URL=sqlite:///./backend/data/santong.db
DEFAULT_ACTOR_CODE=ADMIN001
PUBLIC_BASE_URL=http://127.0.0.1:8000
TIMEZONE=Asia/Taipei
LINE_CHANNEL_SECRET=
LINE_CHANNEL_ACCESS_TOKEN=
GOOGLE_SERVICE_ACCOUNT_JSON=
GOOGLE_DRIVE_WORKLOG_FOLDER_ID=1j82gzF2AkiHvJ6sv1ecLvN1E0vQff9F1
GOOGLE_DRIVE_PUBLIC_SHARE=true
DAILY_PUSH_HOUR=7
DAILY_PUSH_MINUTE=0
DEFAULT_PASSWORD=Santong@2026
LOGIN_FAIL_LIMIT=3
LOGIN_LOCK_MINUTES=15
SESSION_EXPIRE_MINUTES=480
SESSION_SECRET_KEY=
```

### Google Drive 工作相片

- `GOOGLE_DRIVE_WORKLOG_FOLDER_ID` 已對應 `三通工程行_工作相片`
- `GOOGLE_SERVICE_ACCOUNT_JSON` 可放 service account JSON 內容，或本機 JSON 檔案絕對路徑
- 需要先把 `三通工程行_工作相片` 分享給 service account 信箱，否則 FastAPI 雖然能收到 LINE 圖片，仍無法寫入 Drive
- 啟用後，綁定員工直接從 LINE 傳圖片，系統會自動建立 `YYYY-MM-DD` 子資料夾、上傳照片，並把雲端連結寫回系統資料庫

## Demo 測試資料

系統第一次啟動會自動建立下列測試資料：

- 工地：`45`, `齊裕53`, `56`, `善捷47`, `金駿76`, `桃園28`, `桃園29`, `新竹寶山1`, `新竹寶山2`, `新竹寶山3`
- 員工（8 人）：
  - `BOSS001` 三通工程行林老闆（owner，可登入後台）
  - `ADMIN001` 林金谷（admin，可登入後台）
  - `BOT001` 三通工程行 line 機器人（external，系統帳號）
  - `ADMIN002` 秀蓉（admin，可登入後台）
  - `EMP001` 勝忠（employee）
  - `EMP002` 小咪（employee）
  - `EMP003` 建成（employee）
  - `EMP004` 林小咪（employee）

對應的 LINE 綁定碼：

- `ST-1001` ~ `ST-1008`

## 後台登入

後台採帳號密碼登入，僅 `owner` 與 `admin` 角色可存取。

- 可登入帳號：`BOSS001`（老闆）、`ADMIN001`（系統管理者）、`ADMIN002`（行政人員）
- 統一預設密碼：`Santong@2026`（首次登入後強制改密碼）
- 登入失敗 3 次鎖定 15 分鐘
- Session 以簽名 cookie `santong_session` 驗證（httponly + samesite=lax，production 下 secure）

## 後台操作方式

首頁可直接完成：

- 建立每日工作安排
- 發送臨時通知
- 建立會議記錄並可同步發送摘要
- 查看待審請假並核准／退回
- 查詢當日考勤
- 模擬 LINE 訊息測試流程
- 查看 LINE 綁定狀態與部署 Rich Menu

## 正式上線前建議

第一版上線前，建議優先補這幾件：

1. ~~將 SQLite 改為 PostgreSQL 正式資料庫~~（已完成，Render 正式站使用 PostgreSQL）
2. ~~將後台模擬身分切換改為正式登入~~（已完成，8/7 上線統一預設密碼 + 強制改密碼 + 角色範圍 + 失敗鎖定）
3. ~~串接 LINE Rich Menu 與 webhook 正式 channel~~（已完成）
4. ~~補上 Google Drive / Calendar 整合~~（Google Drive 工作相片上傳已完成）
5. 將既有 Excel 的員工、工地、排班、收支資料做匯入腳本
6. 將考勤擴充到 GPS / QR Code / 拍照打卡
7. `reset-password` 端點加權限保護（目前未要求登入，作為緊急復原工具）
8. 後台使用者管理 UI（新增/停用帳號、重設密碼按鈕）

## 官方文件依據

本版實作方向對齊：

- LINE Messaging API 官方文件：webhook 驗簽、接收訊息、push message
- FastAPI 官方文件：API 與 SQL database 結構
- SQLModel 官方文件：資料模型與 ORM 寫法
