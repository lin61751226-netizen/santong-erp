"""Copy a verified SQLite snapshot to a new persistent-disk path, never overwrite it."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


CORE_TABLES = (
    "employee",
    "loginlog",
    "attendanceevent",
    "forkliftinspection",
    "photouploadlog",
    "workreportevent",
    "signsliprecord",
    "financeentry",
)


def inspect_database(path: Path) -> dict[str, int]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"資料庫不存在或為空：{path}")
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError(f"資料庫完整性檢查失敗：{path}")
            tables = {
                row[0].lower()
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "employee" not in tables:
                raise ValueError(f"缺少員工資料表：{path}")
            counts = {
                table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                if table in tables else 0
                for table in CORE_TABLES
            }
            if counts["employee"] == 0:
                raise ValueError(f"員工資料表為空：{path}")
            return counts
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"資料庫無法讀取：{path}") from exc


def prepare_persistent_database(source: Path, target: Path) -> dict:
    source = source.resolve()
    target = target.resolve()
    if source == target:
        raise ValueError("來源與目標不可相同")
    if target.exists():
        raise FileExistsError(f"目標已存在，不會覆蓋：{target}")
    if not target.parent.is_dir():
        raise ValueError(f"目標目錄不存在：{target.parent}")
    source_counts = inspect_database(source)

    descriptor, staging_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".staging", dir=target.parent)
    os.close(descriptor)
    staging = Path(staging_name)
    try:
        with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as source_db:
            with closing(sqlite3.connect(staging)) as target_db:
                source_db.backup(target_db)
        copied_counts = inspect_database(staging)
        if copied_counts != source_counts:
            raise ValueError("搬遷後核心紀錄筆數不一致")
        os.link(staging, target)
        return {"status": "prepared", "bytes": target.stat().st_size, "counts": copied_counts}
    finally:
        staging.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="已核對的 SQLite 備份來源")
    parser.add_argument("target", type=Path, help="持久磁碟上的全新目標檔")
    args = parser.parse_args()
    print(json.dumps(prepare_persistent_database(args.source, args.target), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
