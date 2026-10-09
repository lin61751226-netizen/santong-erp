#!/usr/bin/env python3
"""把範例句字送給自然語言解析器，只印出意圖，不寫入資料庫。

用法（在 backend 目錄，或直接用檔案路徑）：

    OPENAI_API_KEY=... python scripts/ai_smoke_test.py
    OPENAI_API_KEY=... python scripts/ai_smoke_test.py "後天我要請事假"

金鑰請放在環境變數，不要寫進這個檔案。AI_ASSISTANT_ENABLED 不必打開，這個腳本不走 LINE。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.services.ai_assistant import AiServiceUnavailable, parse_user_text  # noqa: E402
from app.services.forklift_service import local_today  # noqa: E402


EXAMPLES = (
    "明天 47 標要兩台堆高機，勝忠跟建成去",
    "我下週一想排休",
    "10/20 請病假",
    "今天善捷工地做完了，堆高機搬了三車鋼筋",
    "上班",
    "我到工地了",
    "明天我去哪個工地？",
)


def _sentences() -> list[str]:
    extra = [item.strip() for item in sys.argv[1:] if item.strip()]
    return list(EXAMPLES) + extra


async def _run() -> int:
    if not settings.openai_api_key.strip():
        print("請先在環境變數設定 OPENAI_API_KEY。這個腳本不會讀取或印出金鑰，也不會寫入資料庫。")
        return 2
    today = local_today()
    print(f"模型：{settings.openai_model}")
    print(f"今天（Asia/Taipei）：{today.isoformat()}")
    print("只解析意圖，不寫入資料庫。")
    failed = 0
    for sentence in _sentences():
        print("\n" + "=" * 40)
        print(f"句子：{sentence}")
        try:
            parsed = await parse_user_text(sentence, today=today, timeout=25)
        except AiServiceUnavailable as exc:
            failed += 1
            print(f"解析失敗：{type(exc).__name__}")
            continue
        except Exception:
            failed += 1
            print(f"解析失敗：{sys.exc_info()[0].__name__}")
            continue
        print(json.dumps(parsed.model_dump(), ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
