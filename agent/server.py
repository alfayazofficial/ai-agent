import logging
import os
import threading
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from . import campaign
from .config import Business, Settings
from .db import DB
from .engine import Conversation, Turn
from .llm import LLM
from .notify import post_call_event
from .twilio_utils import (place_call, twiml_say_dial, twiml_say_gather, twiml_say_hangup, valid_signature)

log = logging.getLogger("agent.server")
XML = "application/xml"


def create_app(biz: Business | None = None, settings: Settings | None = None, db: DB | None = None,
               llm: LLM | None = None) -> FastAPI:
    biz = biz or Business.load()
    cfg = settings or Settings.from_env()
    db = db or DB(cfg.db_path)
    llm = llm or LLM(cfg.llm_base_url, cfg.llm_model, cfg.llm_api_key)
    sessions: dict[str, Conversation] = {}
    lock = threading.Lock()
    app = FastAPI(title=f"{biz.name} AI voice agent")
    lang = cfg.speech_language

    # ---------- helpers ----------
    def admin(x_api_key: str = Header(default="")):
        if not cfg.api_key or x_api_key != cfg.api_key:
            raise HTTPException(401, "invalid or missing X-API-Key")

    async def twilio_form(request: Request) -> dict:
        form = dict(await request.form())
        if cfg.twilio_token:
            base = cfg.public_base_url or str(request.base_url).rstrip("/")
            url = base + request.url.path + (f"?{request.url.query}" if request.url.query else "")
            if not valid_signature(cfg.twilio_token, url, form, request.headers.get("X-Twilio-Signature", "")):
                raise HTTPException(403, "bad Twilio signature")
        return form

    def finish(sid: str):
        with lock:
            conv = sessions.pop(sid, None)
        if conv:
            call = conv.finish()
            if call:
                post_call_event(cfg.n8n_webhook_url, call,
                                [a for a in db.appointments() if a["call_id"] == call["id"]],
                                [m for m in db.messages() if m["call_id"] == call["id"]])

    def reply_xml(turn: Turn, sid: str) -> Response:
        if turn.transfer:
            body = twiml_say_dial(turn.say, turn.transfer, lang)
        elif turn.end:
            body = twiml_say_hangup(turn.say, lang)
        else:
            body = twiml_say_gather(turn.say, lang)
        if turn.end or turn.transfer:
            finish(sid)
        return Response(body, media_type=XML)

    def new_conv(sid, phone, direction="inbound", contact=None) -> Conversation:
        contact = contact or db.get_contact(phone)
        conv = Conversation(biz, db, llm, sid, phone, direction, contact)
        with lock:
            sessions[sid] = conv
        return conv

    # ---------- Twilio voice webhooks ----------
    @app.post("/voice/inbound")
    async def voice_inbound(request: Request):
        f = await twilio_form(request)
        sid, phone = f.get("CallSid", uuid.uuid4().hex), f.get("From", "unknown")
        return reply_xml(new_conv(sid, phone).greeting(), sid)

    @app.post("/voice/outbound")
    async def voice_outbound(request: Request, contact_id: int = 0):
        f = await twilio_form(request)
        sid = f.get("CallSid", uuid.uuid4().hex)
        contact = db.get_contact_by_id(contact_id) or {}
        phone = contact.get("phone") or f.get("To", "unknown")
        return reply_xml(new_conv(sid, phone, "outbound", contact or None).greeting(), sid)

    @app.post("/voice/gather")
    async def voice_gather(request: Request):
        f = await twilio_form(request)
        sid = f.get("CallSid", "")
        conv = sessions.get(sid) or new_conv(sid, f.get("From", "unknown"))
        return reply_xml(conv.respond(f.get("SpeechResult", "")), sid)

    @app.post("/voice/status")
    async def voice_status(request: Request):
        f = await twilio_form(request)
        finish(f.get("CallSid", ""))
        return JSONResponse({"ok": True})

    # ---------- browser demo (100% free: Web Speech API does STT + TTS) ----------
    class Chat(BaseModel):
        session_id: str
        text: str = ""
        start: bool = False

    @app.post("/api/chat")
    def chat(body: Chat):
        conv = sessions.get(body.session_id)
        if body.start or not conv:
            conv = new_conv(body.session_id, "web-demo")
            turn = conv.greeting()
        else:
            turn = conv.respond(body.text)
        if turn.end or turn.transfer:
            finish(body.session_id)
        return {"say": turn.say, "end": turn.end, "transfer": turn.transfer}

    @app.get("/demo", response_class=HTMLResponse)
    def demo():
        return (Path(__file__).parent / "demo.html").read_text()

    # ---------- admin API ----------
    class Outbound(BaseModel):
        phone: str
        name: str = ""
        consent: bool = False

    @app.post("/api/outbound", dependencies=[Depends(admin)])
    def outbound(body: Outbound):
        if not (cfg.twilio_sid and cfg.twilio_token and cfg.public_base_url):
            raise HTTPException(400, "Twilio credentials and PUBLIC_BASE_URL are required for real calls")
        contact = db.upsert_contact(body.name, body.phone, "", body.consent)
        ok, why = campaign.can_call(db, biz, contact)
        if not ok:
            raise HTTPException(403, f"call blocked: {why}")
        sid = place_call(cfg.twilio_sid, cfg.twilio_token, cfg.twilio_number, contact["phone"],
                         f"{cfg.public_base_url}/voice/outbound?contact_id={contact['id']}",
                         f"{cfg.public_base_url}/voice/status")
        return {"call_sid": sid}

    @app.post("/api/campaign/run", dependencies=[Depends(admin)])
    def run_campaign(tasks: BackgroundTasks):
        targets = campaign.eligible(db, biz)
        tasks.add_task(_dial_all, targets)
        return {"queued": len(targets)}

    def _dial_all(targets):
        import time as _t
        for c in targets:
            try:
                place_call(cfg.twilio_sid, cfg.twilio_token, cfg.twilio_number, c["phone"],
                           f"{cfg.public_base_url}/voice/outbound?contact_id={c['id']}",
                           f"{cfg.public_base_url}/voice/status")
            except Exception as exc:
                log.warning("dial failed for %s: %s", c["phone"], exc)
            _t.sleep(5)   # pace calls; respect carrier limits

    @app.get("/api/calls", dependencies=[Depends(admin)])
    def calls():
        return db.calls()

    @app.get("/api/appointments", dependencies=[Depends(admin)])
    def appointments():
        return db.appointments()

    @app.get("/api/messages", dependencies=[Depends(admin)])
    def messages():
        return db.messages()

    @app.get("/health")
    def health():
        return {"ok": True, "llm": llm.enabled, "business": biz.name}

    app.state.sessions, app.state.db = sessions, db
    return app


def get_app() -> FastAPI:  # uvicorn --factory agent.server:get_app
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    return create_app()
