"""Call brain: one Conversation per phone call.

Order of precedence each turn:
  1. Hard compliance rules (stop-calling / human request) - never left to the LLM.
  2. LLM (Ollama/Groq/...) returning {"say", "action", "end"} JSON.
  3. Offline rule engine if no LLM is configured or it fails, so a call never dead-ends.
"""
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import Business
from .db import DB
from .llm import LLM, extract_json
from .nlu import (parse_date, parse_phone, parse_time, says_goodbye, says_human, says_stop)

MAX_SILENCE = 2


@dataclass
class Turn:
    say: str
    end: bool = False
    transfer: str | None = None


def fmt_time(t: time) -> str:
    h = t.hour % 12 or 12
    ap = "AM" if t.hour < 12 else "PM"
    return f"{h}:{t.minute:02d} {ap}" if t.minute else f"{h} {ap}"


def fmt_day(d: date) -> str:
    return d.strftime("%A %B ") + str(d.day)


def extract_name(text: str) -> str:
    t = re.sub(r"(?i)\b(my name is|this is|i am|i'm|it's|its|call me|name's)\b", "", text)
    words = re.findall(r"[A-Za-z'\-]+", t)
    return " ".join(w.capitalize() for w in words[:3])


class Conversation:
    def __init__(self, biz: Business, db: DB, llm: LLM, call_sid: str, phone: str,
                 direction: str = "inbound", contact: dict | None = None, clock=datetime.now):
        self.biz, self.db, self.llm = biz, db, llm
        self.call_sid, self.phone, self.direction = call_sid, phone, direction
        self.contact = contact
        self.clock = clock
        self.history: list[dict] = []
        self.mode: str | None = None          # fallback flow: "book" | "message"
        self.awaiting: str | None = None
        self.slots: dict = {}
        self.silence = 0
        self.outcome: str | None = None
        self.engaged = direction == "inbound"
        self.done = False
        self.call_id = db.start_call(call_sid, direction, phone)

    # ---------- public API ----------
    def greeting(self) -> Turn:
        b = self.biz
        if self.direction == "outbound":
            who = f" Am I speaking with {self.contact['name']}?" if self.contact and self.contact.get("name") else ""
            purpose = b.outbound.get("purpose", "following up with you")
            text = (f"Hello, this is {b.agent_name}, an automated AI assistant calling from {b.name}, "
                    f"{purpose}.{who}")
        else:
            text = (f"Thank you for calling {b.name}. I'm {b.agent_name}, an automated AI assistant. "
                    "How can I help you today?")
        return self._say(text)

    def respond(self, user_text: str) -> Turn:
        text = (user_text or "").strip()
        if not text:
            self.silence += 1
            if self.silence > MAX_SILENCE:
                return self._end("Sorry, I can't hear you. Please call us back any time. Goodbye.", "no_response")
            return self._say("Sorry, I didn't catch that. Could you say it again?")
        self.silence = 0
        self.history.append({"role": "user", "text": text})

        if says_stop(text):
            self.db.add_dnc(self.phone)
            return self._end("Understood. I've removed your number and you won't be called again. Goodbye.", "do_not_call")
        if says_human(text):
            if self.biz.transfer_number:
                self.outcome = "transferred"
                return self._say("One moment, I'll connect you to a team member.", transfer=self.biz.transfer_number)
            self.mode, self.awaiting = "message", "name" if not self._known_name() else "body"
            return self._say("Nobody is available right now, but I can take a message and have someone call you back. "
                             + ("What's your name?" if self.awaiting == "name" else "What would you like me to pass on?"))

        turn = self._llm_turn() if self.llm.enabled else None
        return turn or self._fallback(text)

    def finish(self) -> dict | None:
        """Persist transcript + summary. Safe to call more than once."""
        if self.done:
            return None
        self.done = True
        outcome = self.outcome or ("answered_questions" if any(h["role"] == "user" for h in self.history) else "no_response")
        summary = self._summarize(outcome)
        return self.db.end_call(self.call_sid, self.history, summary, outcome)

    # ---------- helpers ----------
    def _say(self, text: str, end: bool = False, transfer: str | None = None) -> Turn:
        self.history.append({"role": "assistant", "text": text})
        return Turn(text, end, transfer)

    def _end(self, text: str, outcome: str | None = None) -> Turn:
        if outcome:
            self.outcome = outcome
        return self._say(text, end=True)

    def _known_name(self) -> str:
        return (self.contact or {}).get("name") or ""

    def _free_slots(self, d: date, n: int = 3) -> list[time]:
        hrs = self.biz.day_hours(d.weekday())
        if not hrs:
            return []
        out, cur = [], datetime.combine(d, hrs[0])
        end = datetime.combine(d, hrs[1])
        step = timedelta(minutes=self.biz.slot_minutes)
        while cur + step <= end and len(out) < n:
            if cur > self.clock() and not self.db.slot_taken(cur, self.biz.slot_minutes):
                out.append(cur.time())
            cur += step
        return out

    def book(self, name: str, d: date, t: time, reason: str = "") -> tuple[bool, str]:
        """Validated booking shared by the LLM tool and the rule engine."""
        now = self.clock()
        if d < now.date():
            return False, "That date has already passed. What day would you like instead?"
        hrs = self.biz.day_hours(d.weekday())
        if not hrs:
            return False, f"We're closed on {d.strftime('%A')}s. Our hours are {self.biz.hours_sentence()}. Which other day works?"
        start = datetime.combine(d, t)
        if start <= now or t < hrs[0] or (datetime.combine(d, t) + timedelta(minutes=self.biz.slot_minutes)) > datetime.combine(d, hrs[1]):
            opts = self._free_slots(d)
            hint = f" I have {', '.join(fmt_time(s) for s in opts)} free." if opts else ""
            return False, f"We can't do {fmt_time(t)} on {fmt_day(d)}.{hint} What time works?"
        if self.db.slot_taken(start, self.biz.slot_minutes):
            opts = self._free_slots(d)
            hint = f" I do have {', '.join(fmt_time(s) for s in opts)}." if opts else " That day is fully booked."
            return False, f"Sorry, {fmt_time(t)} on {fmt_day(d)} is taken.{hint} Which would you like?"
        self.db.add_appointment(name, self.phone, start, self.biz.slot_minutes, reason, self.call_id)
        self.outcome = "appointment_booked"
        return True, f"You're booked, {name}, for {fmt_day(d)} at {fmt_time(t)}. Is there anything else I can help with?"

    def take_message(self, name: str, body: str, callback: str | None = None) -> str:
        self.db.add_message(name, callback or self.phone, body, self.call_id)
        if self.outcome != "appointment_booked":
            self.outcome = "message_taken"
        return f"Thanks {name}, I've passed your message to the team and they'll call you back. Anything else?"

    # ---------- LLM path ----------
    def _system_prompt(self) -> str:
        b, now = self.biz, self.clock()
        role = ("You are on an OUTBOUND call. Open by confirming who you are speaking with and state the purpose briefly. "
                f"Purpose: {b.outbound.get('purpose', 'follow up')}. Contact: {self._known_name() or 'unknown'}. "
                "If they decline or say it's a bad time, thank them and end the call (outcome declined/callback_later)."
                if self.direction == "outbound" else "You are answering an INBOUND call.")
        faqs = "\n".join(f"- Q: {f['q']} A: {f['a']}" for f in b.faqs)
        return f"""You are {b.agent_name}, the AI phone receptionist for {b.name}. {role}
Today is {now.strftime('%A %Y-%m-%d %H:%M')} ({b.timezone}). Caller number: {self.phone}.
Opening hours: {b.hours_sentence()}. Appointments are {b.slot_minutes} minutes. Address: {b.address}.
Services: {', '.join(b.services)}.
FAQs:
{faqs}

Rules: speak in 1-2 short plain sentences (this is a phone call, no lists/markdown). You are an AI and must say so if asked.
Never invent prices, availability or policies - use only the facts above, otherwise offer to take a message.
Collect name, date and time before booking. Never confirm a booking yourself: request it with the action and the system confirms.
Reply ONLY with JSON: {{"say": "<what to speak>", "action": null | {{"name": "book_appointment", "args": {{"name": "", "date": "YYYY-MM-DD", "time": "HH:MM", "reason": ""}}}} | {{"name": "take_message", "args": {{"name": "", "message": "", "callback_number": ""}}}}, "end": false}}
Set "end": true only after the caller has nothing else and you have said goodbye."""

    def _llm_turn(self) -> Turn | None:
        msgs = [{"role": "system", "content": self._system_prompt()}]
        for h in self.history[-16:]:
            msgs.append({"role": h["role"], "content": h["text"]})
        data = extract_json(self.llm.chat(msgs))
        if not data or not isinstance(data.get("say"), str):
            return None
        say, end = data["say"].strip(), bool(data.get("end"))
        act = data.get("action")
        if isinstance(act, dict):
            result = self._run_action(act.get("name"), act.get("args") or {})
            if result:
                say = result
        return self._say(say, end=end)

    def _run_action(self, name: str | None, args: dict) -> str | None:
        try:
            if name == "book_appointment":
                d, t = parse_date(str(args.get("date", ""))), parse_time(str(args.get("time", "")))
                who = args.get("name") or self._known_name()
                if not (d and t and who):
                    return "Sorry, I still need your name, the day and the time to book that. Could you tell me again?"
                return self.book(who, d, t, str(args.get("reason", "")))[1]
            if name == "take_message":
                body, who = str(args.get("message", "")).strip(), args.get("name") or self._known_name() or "caller"
                if not body:
                    return None
                cb = parse_phone(str(args.get("callback_number", "")))
                return self.take_message(who, body, cb)
        except Exception:
            return None
        return None

    # ---------- offline rule engine ----------
    def _fallback(self, text: str) -> Turn:
        low = text.lower()
        if not self.engaged:                       # first reply on an outbound call
            self.engaged = True
            if re.search(r"\b(not interested|no thanks|no thank you|wrong number|\bno\b)\b", low) and not re.search(r"\byes\b", low):
                return self._end("No problem, thanks for your time. Goodbye.", "declined")
            if re.search(r"\b(later|busy|bad time|not a good time|driving)\b", low):
                return self._end("Of course, we'll try again another time. Goodbye.", "callback_later")
            if re.search(r"\b(yes|yeah|yep|sure|ok|okay|please|speaking|this is)\b", low):
                self.mode, self.awaiting, self.slots = "book", None, {}
                return self._book_step(text, first=True)
        if self.mode == "book":
            return self._book_step(text)
        if self.mode == "message":
            return self._message_step(text)
        if re.search(r"\b(book|appointment|schedule|reserve|come in|visit)\b", low):
            self.mode, self.slots = "book", {}
            return self._book_step(text, first=True)
        if re.search(r"\b(message|call me back|callback|call back|leave)\b", low):
            self.mode, self.awaiting = "message", ("body" if self._known_name() else "name")
            return self._say("Sure. " + ("What's your name?" if self.awaiting == "name" else "What would you like me to pass on?"))
        if re.search(r"\b(hours|open|close|closing|when are you)\b", low):
            return self._say(f"Our hours are {self.biz.hours_sentence()}. Would you like to book a visit?")
        faq = self._match_faq(low)
        if faq:
            return self._say(faq + " Anything else I can help with?")
        if says_goodbye(text):
            return self._end(f"Thanks for calling {self.biz.name}. Goodbye!")
        return self._say("I can book an appointment, answer questions about our services and hours, or take a message. What would you like?")

    def _match_faq(self, low: str) -> str | None:
        words = set(re.findall(r"[a-z]{4,}", low))
        best, score = None, 0
        for f in self.biz.faqs:
            s = len(words & set(re.findall(r"[a-z]{4,}", f["q"].lower())))
            if s > score:
                best, score = f["a"], s
        return best if score else None

    def _book_step(self, text: str, first: bool = False) -> Turn:
        s = self.slots
        if self.awaiting == "name":
            s["name"] = extract_name(text)
        else:
            d, t = parse_date(text, self.clock().date()), parse_time(text)
            if d:
                s["date"] = d
            if t:
                s["time"] = t
        if says_goodbye(text) and not first and not s.get("date"):
            self.mode = None
            return self._say("No problem. Is there anything else I can help with?")
        s.setdefault("name", self._known_name())
        if not s.get("date"):
            self.awaiting = "date"
            return self._say("Happy to help. What day would you like to come in?")
        if not s.get("time"):
            self.awaiting = "time"
            opts = self._free_slots(s["date"])
            hint = f" I have {', '.join(fmt_time(o) for o in opts)} available." if opts else ""
            return self._say(f"What time on {fmt_day(s['date'])}?{hint}")
        if not s.get("name"):
            self.awaiting = "name"
            return self._say("And may I have your name?")
        ok, msg = self.book(s["name"], s["date"], s["time"], "appointment")
        if ok:
            self.mode, self.awaiting, self.slots = None, None, {}
        else:
            self.awaiting = "time"
            s.pop("time", None)
            if "closed" in msg or "passed" in msg:
                s.pop("date", None)
                self.awaiting = "date"
        return self._say(msg)

    def _message_step(self, text: str) -> Turn:
        s = self.slots
        if self.awaiting == "name":
            s["name"] = extract_name(text) or "caller"
            self.awaiting = "body"
            return self._say(f"Thanks {s['name']}. What would you like me to pass on?")
        who = s.get("name") or self._known_name() or "caller"
        reply = self.take_message(who, text)
        self.mode, self.awaiting, self.slots = None, None, {}
        return self._say(reply)

    # ---------- summary ----------
    def _summarize(self, outcome: str) -> str:
        convo = "\n".join(f"{h['role']}: {h['text']}" for h in self.history)
        if self.llm.enabled and convo:
            s = self.llm.chat([
                {"role": "system", "content": "Summarize this phone call in one sentence for a CRM note. Plain text only."},
                {"role": "user", "content": convo}])
            if s:
                return s.strip()
        first = next((h["text"] for h in self.history if h["role"] == "user"), "")
        return f"{self.direction.capitalize()} call, outcome: {outcome}." + (f" Caller said: {first[:140]}" if first else "")
