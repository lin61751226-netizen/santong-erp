"""把工地號碼的國字、逐字讀法與同音字收成阿拉伯數字。

只在結果對得上真實工地時才採用。午餐這類詞若前面是「吃、買」等，保持原樣。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.services.bootstrap import SITE_ALIASES

# 標案常被逐字唸。是／市／事與四同音，也與一同音，午與五同音。
_DIGIT_VALUES = {
    "零": "0", "〇": "0", "○": "0",
    "一": "1", "壹": "1", "也": "1",
    "二": "2", "兩": "2", "貳": "2", "贰": "2",
    "三": "3", "參": "3", "参": "3", "叁": "3",
    "四": "4", "肆": "4", "是": "4", "市": "4", "事": "4",
    "五": "5", "伍": "5", "午": "5",
    "六": "6", "陸": "6", "陆": "6",
    "七": "7", "柒": "7",
    "八": "8", "捌": "8",
    "九": "9", "玖": "9",
}
_HOMOPHONE_ONLY = set("是市事也午")
_NUMBER_CHARS = set(_DIGIT_VALUES) | {"十", "拾"}
_CANONICAL = {
    "0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
    "5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
}
_READINGS = {
    "0": ["零"],
    "1": ["一", "也"],
    "2": ["二", "兩"],
    "3": ["三"],
    "4": ["四", "是", "市", "事"],
    "5": ["五", "午"],
    "6": ["六"],
    "7": ["七"],
    "8": ["八"],
    "9": ["九"],
}
_MEAL_BEFORE = set("吃買煮叫用做")
_COUNT_VALUES = {"一": "1", "二": "2", "兩": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
_DATE_PART = r"[零〇○一二三四五六七八九十拾兩]{1,3}"
_DATE_RE = re.compile(rf"({_DATE_PART})月({_DATE_PART})[日號]?")
_COUNT_RE = re.compile(r"([一二兩三四五六七八九十])\s*([台臺])")
_ARABIC_RE = re.compile(r"\d+")


@dataclass
class SpeechFix:
    plain: str
    display: str
    changed: bool


def site_numbers(sites) -> set[str]:
    found: set[str] = set()
    for site in sites:
        blob = f"{getattr(site, 'code', '') or ''} {getattr(site, 'name', '') or ''}"
        found.update(_ARABIC_RE.findall(blob))
    return found


def worksite_prompt_labels(sites) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for number in sorted(site_numbers(sites), key=lambda item: (len(item), item)):
        label = f"{number}標"
        if label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def transcription_examples(numbers: set[str]) -> str:
    bits: list[str] = []
    for number in sorted(numbers, key=lambda item: (len(item), item)):
        if len(number) < 2 or any(digit not in _CANONICAL for digit in number):
            continue
        spoken = "".join(_CANONICAL[digit] for digit in number)
        bits.append(f"{spoken}寫成{number}標")
        if len(bits) >= 6:
            break
    if "53" in numbers:
        bits.append("午餐寫成53標")
    if "47" in numbers:
        bits.append("市七寫成47標")
    return "、".join(bits)


def spoken_numbers(query: str) -> list[str]:
    """一個工地說法可能對到的阿拉伯數字。還沒核對工地名單。"""
    cleaned = unicodedata.normalize("NFKC", query or "")
    cleaned = re.sub(r"\s+", "", cleaned)
    found: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in found:
            found.append(value)

    for match in re.finditer(r"午餐|五餐|午三", cleaned):
        if match.start() > 0 and cleaned[match.start() - 1] in _MEAL_BEFORE:
            continue
        add("53")

    for run in _runs(cleaned):
        if "十" in run or "拾" in run:
            parsed = _parse_ten(run)
            if parsed is not None:
                add(str(parsed))
            if run in {"十", "拾"} and "標" in cleaned:
                add("4")
            continue
        if not run or any(char not in _DIGIT_VALUES for char in run):
            continue
        if len(run) == 1 and run in _HOMOPHONE_ONLY and "標" not in cleaned:
            continue
        add("".join(_DIGIT_VALUES[char] for char in run))
    return found


def sites_for_number(sites, number: str) -> list:
    if not number:
        return []
    pattern = re.compile(rf"(?<!\d){re.escape(number)}(?!\d)")
    alias = SITE_ALIASES.get(number)
    chosen = []
    seen: set[int] = set()
    for site in sites:
        site_id = getattr(site, "id", None)
        blob = f"{getattr(site, 'code', '') or ''} {getattr(site, 'name', '') or ''}"
        matched = bool(pattern.search(blob))
        if alias and (getattr(site, "name", None) == alias or getattr(site, "code", None) == alias):
            matched = True
        if not matched:
            continue
        key = site_id if isinstance(site_id, int) else id(site)
        if key in seen:
            continue
        seen.add(key)
        chosen.append(site)
    return chosen


def match_spoken_sites(query: str, active: list, inactive: list) -> tuple[str, list]:
    numbers = spoken_numbers(query)
    if not numbers:
        return "missing", []
    found = []
    seen: set[int] = set()
    for number in numbers:
        for site in sites_for_number(active, number):
            key = site.id if isinstance(getattr(site, "id", None), int) else id(site)
            if key in seen:
                continue
            seen.add(key)
            found.append(site)
    if len(found) == 1:
        return "resolved", found
    if len(found) > 1:
        return "choices", found
    inactive_found = []
    inactive_seen: set[int] = set()
    for number in numbers:
        for site in sites_for_number(inactive, number):
            key = site.id if isinstance(getattr(site, "id", None), int) else id(site)
            if key in inactive_seen:
                continue
            inactive_seen.add(key)
            inactive_found.append(site)
    if len(inactive_found) == 1:
        return "inactive", inactive_found[:1]
    return "missing", []


def correct_spoken_text(text: str, sites) -> SpeechFix:
    """改寫整句語音。plain 給解析器，display 用【】標出改過的地方。"""
    original = text or ""
    numbers = site_numbers(sites)
    spans: list[tuple[int, int, str]] = []
    for match in _DATE_RE.finditer(original):
        month = _parse_ten(match.group(1))
        day = _parse_ten(match.group(2))
        if month is None or day is None or not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        spans.append((match.start(), match.end(), f"{month}月{day}日"))
    for phrase, replacement in _site_phrases(numbers):
        start = 0
        while True:
            index = original.find(phrase, start)
            if index < 0:
                break
            end = index + len(phrase)
            start = index + 1
            if _overlaps(index, end, spans):
                continue
            if not _boundary(original, index, end):
                continue
            if phrase.startswith(("午餐", "五餐")) and index > 0 and original[index - 1] in _MEAL_BEFORE:
                continue
            spans.append((index, end, replacement))
    for match in _COUNT_RE.finditer(original):
        if _overlaps(match.start(), match.end(), spans):
            continue
        spans.append((match.start(), match.end(), f"{_COUNT_VALUES[match.group(1)]}台"))
    if not spans:
        return SpeechFix(original, original, False)
    plain_parts: list[str] = []
    display_parts: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(spans):
        plain_parts.append(original[cursor:start])
        display_parts.append(original[cursor:start])
        plain_parts.append(replacement)
        display_parts.append(f"【{replacement}】")
        cursor = end
    plain_parts.append(original[cursor:])
    display_parts.append(original[cursor:])
    plain = "".join(plain_parts)
    display = "".join(display_parts)
    return SpeechFix(plain, display, plain != original)


def _site_phrases(numbers: set[str]) -> list[tuple[str, str]]:
    phrases: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(phrase: str, number: str) -> None:
        if not phrase or phrase in seen or phrase.isdigit():
            return
        seen.add(phrase)
        phrases.append((phrase, f"{number}標"))

    for number in numbers:
        if not number.isdigit():
            continue
        readings = [_READINGS[digit] for digit in number if digit in _READINGS]
        if len(readings) != len(number):
            continue
        if len(number) == 1:
            for reading in readings[0]:
                add(reading + "標", number)
            continue
        combos = [""]
        for options in readings:
            combos = [prefix + option for prefix in combos for option in options]
            if len(combos) > 48:
                combos = combos[:48]
                break
        for combo in combos:
            add(combo, number)
            add(combo + "標", number)
        if len(number) == 2 and number[0] != "0":
            tens = _CANONICAL[number[0]]
            ones = "" if number[1] == "0" else _CANONICAL[number[1]]
            formed = f"{tens}十{ones}"
            add(formed, number)
            add(formed + "標", number)
    if "53" in numbers:
        for phrase in ("午餐標", "五餐標", "午餐", "五餐"):
            add(phrase, "53")
    phrases.sort(key=lambda item: len(item[0]), reverse=True)
    return phrases


def _runs(text: str) -> list[str]:
    runs: list[str] = []
    buffer = ""
    for char in text:
        if char in _NUMBER_CHARS:
            buffer += char
            continue
        if buffer:
            runs.append(buffer)
            buffer = ""
    if buffer:
        runs.append(buffer)
    return runs


def _parse_ten(token: str) -> int | None:
    text = (token or "").replace("拾", "十")
    if not text:
        return None
    if "十" not in text:
        if len(text) == 1 and text in _DIGIT_VALUES and text not in _HOMOPHONE_ONLY:
            return int(_DIGIT_VALUES[text])
        return None
    if text.count("十") != 1:
        return None
    left, right = text.split("十")
    if left and (len(left) != 1 or left in _HOMOPHONE_ONLY or left not in _DIGIT_VALUES):
        return None
    if right and (len(right) != 1 or right in _HOMOPHONE_ONLY or right not in _DIGIT_VALUES):
        return None
    tens = int(_DIGIT_VALUES[left]) if left else 1
    ones = int(_DIGIT_VALUES[right]) if right else 0
    if tens < 1:
        return None
    return tens * 10 + ones


def _boundary(text: str, start: int, end: int) -> bool:
    if start > 0 and text[start - 1] in _NUMBER_CHARS:
        return False
    if end < len(text) and text[end] in _NUMBER_CHARS:
        return False
    return True


def _overlaps(start: int, end: int, spans: list[tuple[int, int, str]]) -> bool:
    return any(not (end <= left or start >= right) for left, right, _replacement in spans)
