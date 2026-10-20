"""Outbound campaigns with built-in compliance gates (consent, DNC, calling window)."""
import csv
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Business, parse_range
from .db import DB, norm_phone


def in_calling_window(biz: Business, now: datetime | None = None) -> bool:
    now = (now or datetime.now(ZoneInfo(biz.timezone))).astimezone(ZoneInfo(biz.timezone))
    lo, hi = parse_range(biz.outbound.get("calling_window", "09:00-19:00"))
    return lo <= now.time() <= hi


def load_csv(db: DB, path: str) -> int:
    """CSV columns: name,phone,consent(yes/no),notes. Rows without explicit consent are stored but never dialled."""
    n = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("phone"):
                continue
            consent = str(row.get("consent", "")).strip().lower() in {"yes", "y", "true", "1"}
            db.upsert_contact(row.get("name", ""), row["phone"], row.get("notes", ""), consent)
            n += 1
    return n


def can_call(db: DB, biz: Business, contact: dict, now: datetime | None = None) -> tuple[bool, str]:
    if not contact.get("consent"):
        return False, "no recorded consent"
    if db.is_dnc(contact["phone"]):
        return False, "on do-not-call list"
    if not in_calling_window(biz, now):
        return False, "outside calling window"
    return True, "ok"


def eligible(db: DB, biz: Business) -> list[dict]:
    rows = db._all("SELECT * FROM contacts ORDER BY id")
    return [c for c in rows if can_call(db, biz, c)[0] and norm_phone(c["phone"])]
