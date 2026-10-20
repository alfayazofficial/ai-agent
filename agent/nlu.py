"""Tiny date/time/intent parsing. Used by the offline engine and to validate LLM tool args."""
import re
from datetime import date, datetime, time, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_date(text: str, today: date | None = None) -> date | None:
    today = today or date.today()
    t = text.lower()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    if "day after tomorrow" in t:
        return today + timedelta(days=2)
    if "tomorrow" in t:
        return today + timedelta(days=1)
    if re.search(r"\btoday\b", t):
        return today
    for i, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}\b|\b{name[:3]}\b", t):
            delta = (i - today.weekday()) % 7 or 7
            return today + timedelta(days=delta)
    return None


def parse_time(text: str) -> time | None:
    t = text.lower().replace(".", "")
    if "noon" in t:
        return time(12, 0)
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    if m:
        h, mi, ap = int(m[1]), int(m[2] or 0), m[3]
        if not 1 <= h <= 12 or mi > 59:
            return None
        h = h % 12 + (12 if ap == "pm" else 0)
        return time(h, mi)
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t)
    if m:
        return time(int(m[1]), int(m[2]))
    m = re.search(r"\bat\s+(\d{1,2})\b", t)
    if m:
        h = int(m[1])
        if 1 <= h <= 23:
            return time(h + 12 if 1 <= h <= 6 else h, 0)  # "at 3" -> 3pm
    return None


def parse_phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text)
    return digits if 7 <= len(digits) <= 15 else None


def says_stop(text: str) -> bool:
    return bool(re.search(r"\b(stop calling|do not call|don'?t call|remove me|unsubscribe|take me off)\b", text.lower()))


def says_human(text: str) -> bool:
    return bool(re.search(r"\b(human|real person|representative|operator|manager|speak to (someone|somebody))\b", text.lower()))


def says_goodbye(text: str) -> bool:
    return bool(re.search(r"\b(bye|goodbye|that'?s all|nothing else|no thanks|no thank you|i'?m done)\b", text.lower()))


def combine(d: date, t: time) -> datetime:
    return datetime.combine(d, t)
