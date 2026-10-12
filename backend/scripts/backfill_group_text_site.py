#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群組文字工地重新歸類（回填／修正工具）。

通常不須手動執行：後台「整理工作日誌」打開該日時，系統已會自動以最新考勤與
派工事實重新歸類（只認考勤事實／有效派工，不憑主工地亂猜，人工改過的會鎖定）。
本腳本適用於 Render shell 或需逐日批次補修歷史資料時使用。

用法（在 backend 目錄）：

    # 預覽某日會變更哪些紀錄（不寫庫）
    python scripts/backfill_group_text_site.py --date 2026-10-12 --dry-run

    # 正式重新歸類某日
    python scripts/backfill_group_text_site.py --date 2026-10-12

    # 人工把單筆紀錄改歸到指定工地（如老闆沒打到達工地、系統無事實可判）
    python scripts/backfill_group_text_site.py --id 548 --site 2
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session, select  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.models import Employee, Role, Worksite  # noqa: E402
from app.services.google_drive import google_drive_worklog_service  # noqa: E402
from app.services.group_text_logs import (  # noqa: E402
    GroupTextReassignError,
    reassign_group_text_log,
    reassign_group_texts_for_date,
)


def _system_owner(session: Session) -> Employee | None:
    owner = session.exec(
        select(Employee).where(Employee.role == Role.owner).order_by(Employee.id)
    ).first()
    return owner or session.exec(select(Employee).order_by(Employee.id)).first()


def _print_result(result: dict) -> None:
    print(f"日期 {result['work_date']}：檢查 {result['checked']} 筆，修正 {result['updated_count']} 筆")
    for row in result["updated"]:
        print(
            f"  #{row['id']} {row['sent_at']} {row['from_site_name']} → "
            f"{row['to_site_name']}（依據 {row['reason']}）｜{row['content']}"
        )
    if result["unresolved_ids"]:
        print(f"  保留未動（無事實／未綁定／人工鎖定）：{result['unresolved_ids']}")


async def _backup() -> None:
    try:
        backup = await google_drive_worklog_service.backup_database()
        print(f"Google Drive 備份：{backup.get('status')}")
    except Exception as exc:  # noqa: BLE001
        print(f"警告：備份失敗（不影響本次寫入）：{type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="群組文字工地重新歸類")
    parser.add_argument("--date", help="當地日期 YYYY-MM-DD（預設今天）")
    parser.add_argument("--dry-run", action="store_true", help="只預覽不寫庫")
    parser.add_argument("--id", type=int, help="人工改工地：群組文字紀錄 id")
    parser.add_argument("--site", type=int, help="人工改工地：目標工地 id")
    args = parser.parse_args()

    with Session(engine) as session:
        if args.id is not None:
            if not args.site:
                print("人工改工地需同時提供 --id 與 --site")
                return 2
            actor = _system_owner(session)
            try:
                log = reassign_group_text_log(session, args.id, args.site, actor)
            except GroupTextReassignError as exc:
                print(f"失敗：{exc}")
                return 1
            site = session.get(Worksite, args.site)
            print(f"已將紀錄 #{args.id} 改歸至「{site.name}」並鎖定（id={log.id}）")
            asyncio.run(_backup())
            return 0

        target = date.fromisoformat(args.date) if args.date else date.today()
        actor = None if args.dry_run else _system_owner(session)
        result = reassign_group_texts_for_date(session, target, actor, dry_run=args.dry_run)
        _print_result(result)
        if args.dry_run:
            print("（dry-run，未寫庫）")
        elif result["updated_count"]:
            asyncio.run(_backup())
        else:
            print("無需變更。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
