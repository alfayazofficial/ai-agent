import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY, name TEXT, phone TEXT UNIQUE, notes TEXT, consent INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS calls(
  id INTEGER PRIMARY KEY, call_sid TEXT UNIQUE, direction TEXT, phone TEXT,
  started TEXT, ended TEXT, transcript TEXT, summary TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS appointments(
  id INTEGER PRIMARY KEY, name TEXT, phone TEXT, start TEXT, end TEXT, reason TEXT, call_id INTEGER);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, name TEXT, phone TEXT, body TEXT, created TEXT, call_id INTEGER);
CREATE TABLE IF NOT EXISTS dnc(phone TEXT PRIMARY KEY, created TEXT);
"""


def norm_phone(p: str) -> str:
    p = (p or "").strip()
    return ("+" if p.startswith("+") else "") + re.sub(r"\D", "", p)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # contacts / compliance
    def upsert_contact(self, name, phone, notes="", consent=False):
        phone = norm_phone(phone)
        self._exec(
            "INSERT INTO contacts(name,phone,notes,consent) VALUES(?,?,?,?) "
            "ON CONFLICT(phone) DO UPDATE SET name=excluded.name, notes=excluded.notes, consent=excluded.consent",
            (name, phone, notes, int(consent)),
        )
        return self.get_contact(phone)

    def get_contact(self, phone):
        r = self._all("SELECT * FROM contacts WHERE phone=?", (norm_phone(phone),))
        return r[0] if r else None

    def get_contact_by_id(self, cid):
        r = self._all("SELECT * FROM contacts WHERE id=?", (cid,))
        return r[0] if r else None

    def add_dnc(self, phone):
        self._exec("INSERT OR IGNORE INTO dnc(phone,created) VALUES(?,?)", (norm_phone(phone), now()))

    def is_dnc(self, phone):
        return bool(self._all("SELECT 1 FROM dnc WHERE phone=?", (norm_phone(phone),)))

    # calls
    def start_call(self, call_sid, direction, phone):
        self._exec(
            "INSERT OR IGNORE INTO calls(call_sid,direction,phone,started,transcript) VALUES(?,?,?,?,'[]')",
            (call_sid, direction, norm_phone(phone), now()),
        )
        return self._all("SELECT id FROM calls WHERE call_sid=?", (call_sid,))[0]["id"]

    def end_call(self, call_sid, transcript, summary, outcome):
        self._exec(
            "UPDATE calls SET ended=?, transcript=?, summary=?, outcome=? WHERE call_sid=?",
            (now(), json.dumps(transcript), summary, outcome, call_sid),
        )
        r = self._all("SELECT * FROM calls WHERE call_sid=?", (call_sid,))
        return r[0] if r else None

    def calls(self, limit=50):
        return self._all("SELECT * FROM calls ORDER BY id DESC LIMIT ?", (limit,))

    # appointments
    def appointments(self, day=None):
        if day:
            return self._all("SELECT * FROM appointments WHERE substr(start,1,10)=? ORDER BY start", (day,))
        return self._all("SELECT * FROM appointments ORDER BY start")

    def slot_taken(self, start: datetime, minutes: int) -> bool:
        end = start + timedelta(minutes=minutes)
        rows = self._all("SELECT 1 FROM appointments WHERE start < ? AND end > ?", (end.isoformat(), start.isoformat()))
        return bool(rows)

    def add_appointment(self, name, phone, start: datetime, minutes: int, reason, call_id=None):
        end = start + timedelta(minutes=minutes)
        cur = self._exec(
            "INSERT INTO appointments(name,phone,start,end,reason,call_id) VALUES(?,?,?,?,?,?)",
            (name, norm_phone(phone), start.isoformat(), end.isoformat(), reason, call_id),
        )
        return cur.lastrowid

    # messages
    def add_message(self, name, phone, body, call_id=None):
        cur = self._exec(
            "INSERT INTO messages(name,phone,body,created,call_id) VALUES(?,?,?,?,?)",
            (name, norm_phone(phone), body, now(), call_id),
        )
        return cur.lastrowid

    def messages(self):
        return self._all("SELECT * FROM messages ORDER BY id DESC")
