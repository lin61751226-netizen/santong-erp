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
- 角色權限基礎
- LINE webhook 驗簽與文字指令處理
- 每日 07:00 工作安排排程推播
- 可操作的管理後台首頁
- Demo 種子資料

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
DAILY_PUSH_HOUR=7
DAILY_PUSH_MINUTE=0
```

## Demo 測試資料

系統第一次啟動會自動建立下列測試資料：

- 工地：`45`, `53`, `56`, `善捷47`, `金駿76`, `桃園28`, `桃園29`, `新竹寶山1`, `新竹寶山2`, `新竹寶山3`
- 員工：
  - `BOSS001` 三通老闆
  - `ADMIN001` 行政主管
  - `ACC001` 會計小姐
  - `SUP047` 林主任
  - `EMP001` 王小明
  - `EMP002` 李小華
  - `EMP003` 陳志宏

對應的 LINE 綁定碼：

- `ST-1001`
- `ST-1002`
- `ST-1003`
- `ST-1004`
- `ST-1005`
- `ST-1006`
- `ST-1007`

## 後台操作方式

首頁可直接完成：

- 切換操作身分
- 建立每日工作安排
- 發送臨時通知
- 建立會議記錄並可同步發送摘要
- 查看待審請假並核准／退回
- 查詢當日考勤
- 模擬 LINE 訊息測試流程

目前後台登入先採 `X-Actor-Code` 模式模擬角色權限，方便你先跑流程；正式版可再接帳密、Google Workspace SSO 或公司 AD。

## 正式上線前建議

第一版上線前，建議優先補這幾件：

1. 將 SQLite 改為 PostgreSQL 正式資料庫
2. 將後台模擬身分切換改為正式登入
3. 串接 LINE Rich Menu 與 webhook 正式 channel
4. 補上 Google Drive / Calendar 整合
5. 將既有 Excel 的員工、工地、排班、收支資料做匯入腳本
6. 將考勤擴充到 GPS / QR Code / 拍照打卡

## 官方文件依據

本版實作方向對齊：

- LINE Messaging API 官方文件：webhook 驗簽、接收訊息、push message
- FastAPI 官方文件：API 與 SQL database 結構
- SQLModel 官方文件：資料模型與 ORM 寫法
