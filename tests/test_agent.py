import base64
import hashlib
import hmac
from datetime import date, datetime, time

import pytest
from fastapi.testclient import TestClient

from agent import campaign
from agent.config import Business, Settings
from agent.db import DB
from agent.engine import Conversation
from agent.llm import LLM, extract_json
from agent.nlu import parse_date, parse_time
from agent.server import create_app

MON = datetime(2026, 10, 19, 8, 0)   # a Monday morning, before opening


@pytest.fixture
def biz():
    return Business.load("config/business.yaml")


@pytest.fixture
def db():
    return DB(":memory:")


def conv(biz, db, sid="c1", phone="+15551230001", **kw):
    return Conversation(biz, db, LLM(""), sid, phone, clock=lambda: MON, **kw)


def test_parsers():
    today = date(2026, 10, 19)
    assert parse_date("tomorrow please", today) == date(2026, 10, 20)
    assert parse_date("next friday", today) == date(2026, 10, 23)
    assert parse_date("2026-11-02", today) == date(2026, 11, 2)
    assert parse_time("3pm") == time(15, 0)
    assert parse_time("10:30 am") == time(10, 30)
    assert parse_time("at 3") == time(15, 0)
    assert parse_time("14:00") == time(14, 0)
    assert parse_time("nothing here") is None


def test_extract_json_handles_fences():
    assert extract_json('Sure!\n```json\n{"say":"hi"}\n```') == {"say": "hi"}
    assert extract_json("no json") is None


def test_book_appointment_flow(biz, db):
    c = conv(biz, db)
    assert "AI assistant" in c.greeting().say
    assert "What day" in c.respond("I'd like to book an appointment").say
    assert "What time" in c.respond("tomorrow").say
    assert "name" in c.respond("10am").say.lower()
    t = c.respond("my name is jane doe")
    assert "booked" in t.say.lower() and "Jane Doe" in t.say
    appts = db.appointments()
    assert len(appts) == 1 and appts[0]["start"] == "2026-10-20T10:00:00"
    assert c.finish()["outcome"] == "appointment_booked"


def test_double_booking_and_closed_day(biz, db):
    c1 = conv(biz, db, "a")
    c1.respond("book tomorrow at 10am")
    c1.respond("Jane")
    c2 = conv(biz, db, "b", phone="+15551230009")
    t = c2.respond("book tomorrow at 10am")
    t = c2.respond("Bob") if "name" in t.say.lower() else t
    assert "taken" in t.say.lower() and "10:30 AM" in t.say
    assert len(db.appointments()) == 1
    c3 = conv(biz, db, "c")
    assert "closed" in c3.respond("book on saturday at 10am").say.lower() or "closed" in c3.respond("Bob").say.lower()


def test_hours_faq_message_and_goodbye(biz, db):
    c = conv(biz, db)
    assert "Mon 09:00" in c.respond("what are your hours?").say
    assert "80 dollars" in c.respond("how much does it cost?").say
    assert "name" in c.respond("can I leave a message").say.lower()
    c.respond("Sam")
    assert "passed your message" in c.respond("please call me about my invoice").say
    assert db.messages()[0]["body"] == "please call me about my invoice"
    assert c.respond("no thanks, bye").end
    assert c.finish()["outcome"] == "message_taken"


def test_silence_hangup(biz, db):
    c = conv(biz, db)
    assert not c.respond("").end and not c.respond("").end
    assert c.respond("").end
    assert c.finish()["outcome"] == "no_response"


def test_stop_calling_adds_dnc(biz, db):
    c = conv(biz, db, direction="outbound")
    c.greeting()
    t = c.respond("stop calling me")
    assert t.end and db.is_dnc("+15551230001")


def test_human_request_without_transfer_number_takes_message(biz, db):
    t = conv(biz, db).respond("let me talk to a real person")
    assert "message" in t.say.lower()


def test_outbound_decline_and_accept(biz, db):
    contact = db.upsert_contact("Jane", "+15551230001", consent=True)
    c = conv(biz, db, direction="outbound", contact=contact)
    assert "Am I speaking with Jane" in c.greeting().say
    assert c.respond("no thanks not interested").end
    assert c.finish()["outcome"] == "declined"
    c2 = conv(biz, db, "x2", direction="outbound", contact=contact)
    c2.greeting()
    assert "What day" in c2.respond("yes speaking").say


def test_llm_path_and_tool_validation(biz, db, monkeypatch):
    llm = LLM("http://fake")
    replies = iter([
        '{"say":"Booking that now.","action":{"name":"book_appointment","args":{"name":"Ann","date":"2026-10-20","time":"11:00","reason":"cleaning"}},"end":false}',
        '{"say":"Fine!","action":{"name":"book_appointment","args":{"name":"Ann","date":"2026-10-20","time":"11:00"}},"end":false}',
        "garbage not json",
    ])
    monkeypatch.setattr(LLM, "chat", lambda self, m, temperature=0.3: next(replies))
    c = Conversation(biz, db, llm, "l1", "+15551230001", clock=lambda: MON)
    assert "booked" in c.respond("book me tomorrow 11am, I'm Ann").say.lower()
    assert "taken" in c.respond("again").say.lower()          # LLM can't override the double-book check
    assert "hours" in c.respond("what are your hours").say.lower()   # bad JSON -> offline fallback


def test_campaign_compliance(biz, db, tmp_path):
    f = tmp_path / "c.csv"
    f.write_text("name,phone,consent,notes\nA,+15551110001,yes,\nB,+15551110002,no,\nC,+15551110003,yes,\n")
    assert campaign.load_csv(db, str(f)) == 3
    db.add_dnc("+15551110003")
    noon = datetime(2026, 10, 19, 12, 0, tzinfo=__import__("zoneinfo").ZoneInfo(biz.timezone))
    night = noon.replace(hour=23)
    contacts = {c["name"]: c for c in db._all("SELECT * FROM contacts")}
    assert campaign.can_call(db, biz, contacts["A"], noon) == (True, "ok")
    assert campaign.can_call(db, biz, contacts["B"], noon)[1] == "no recorded consent"
    assert campaign.can_call(db, biz, contacts["C"], noon)[1] == "on do-not-call list"
    assert campaign.can_call(db, biz, contacts["A"], night)[1] == "outside calling window"


@pytest.fixture
def client(biz, db):
    return TestClient(create_app(biz, Settings(api_key="k"), db, LLM("")))


def test_twilio_webhooks_end_to_end(client, db):
    r = client.post("/voice/inbound", data={"CallSid": "CA1", "From": "+15559998888"})
    assert r.status_code == 200 and "<Gather" in r.text and "Bright Smile Dental" in r.text
    r = client.post("/voice/gather", data={"CallSid": "CA1", "SpeechResult": "what are your hours"})
    assert "Mon 09:00" in r.text
    r = client.post("/voice/gather", data={"CallSid": "CA1", "SpeechResult": "bye"})
    assert "<Hangup/>" in r.text
    assert db.calls()[0]["call_sid"] == "CA1" and db.calls()[0]["ended"]


def test_twilio_signature_enforced(biz, db):
    app = create_app(biz, Settings(twilio_token="secret", public_base_url="https://x.test"), db, LLM(""))
    c = TestClient(app)
    form = {"CallSid": "CA2", "From": "+1555"}
    assert c.post("/voice/inbound", data=form).status_code == 403
    sig = base64.b64encode(hmac.new(b"secret", ("https://x.test/voice/inbound" + "CallSidCA2From+1555").encode(), hashlib.sha1).digest()).decode()
    assert c.post("/voice/inbound", data=form, headers={"X-Twilio-Signature": sig}).status_code == 200


def test_web_demo_and_admin_auth(client):
    a = client.post("/api/chat", json={"session_id": "s", "start": True}).json()
    assert "AI assistant" in a["say"]
    assert client.post("/api/chat", json={"session_id": "s", "text": "goodbye"}).json()["end"]
    assert client.get("/api/calls").status_code == 401
    assert client.get("/api/calls", headers={"X-API-Key": "k"}).status_code == 200
    assert client.get("/demo").status_code == 200
    assert client.post("/api/outbound", headers={"X-API-Key": "k"}, json={"phone": "+1555"}).status_code == 400
