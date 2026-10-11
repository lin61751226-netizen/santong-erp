"""地址搜尋走 OpenStreetMap Nominatim。遵守每秒最多一次，並標明應用程式。"""
from __future__ import annotations

import threading
import time

import httpx


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "SantongERP/1.0 (worksite map; internal admin)"
MIN_INTERVAL_SECONDS = 1.1

_lock = threading.Lock()
_last_request_monotonic = 0.0


class NominatimError(Exception):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def reset_rate_limit() -> None:
    global _last_request_monotonic
    with _lock:
        _last_request_monotonic = 0.0


def _reserve_slot() -> bool:
    global _last_request_monotonic
    with _lock:
        now = time.monotonic()
        if now - _last_request_monotonic < MIN_INTERVAL_SECONDS:
            return False
        _last_request_monotonic = now
        return True


async def search_address(query: str) -> list[dict]:
    text = " ".join((query or "").split())
    if not text:
        raise NominatimError("請輸入要搜尋的地址", 400)
    if len(text) > 120:
        raise NominatimError("地址請在 120 字以內", 400)
    if not _reserve_slot():
        raise NominatimError("請稍候再搜尋，大約每秒一次", 429)
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            response = await client.get(
                NOMINATIM_URL,
                params={
                    "q": text,
                    "format": "jsonv2",
                    "limit": 5,
                    "countrycodes": "tw",
                    "addressdetails": 0,
                },
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "zh-TW",
                },
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError:
        raise NominatimError("地址搜尋暫時無法使用，請直接在地圖上點選", 502)
    if not isinstance(payload, list):
        raise NominatimError("地址搜尋暫時無法使用，請直接在地圖上點選", 502)
    results = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            latitude = float(item.get("lat"))
            longitude = float(item.get("lon"))
        except (TypeError, ValueError):
            continue
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            continue
        label = str(item.get("display_name") or "").strip()
        if not label:
            continue
        results.append({
            "label": label[:180],
            "latitude": latitude,
            "longitude": longitude,
        })
    return results
