#!/usr/bin/env python3
"""Command line tools: chat simulator, CSV import, outbound campaign, status."""
import argparse
import sys
import uuid

from agent import campaign
from agent.config import Business, Settings
from agent.db import DB
from agent.engine import Conversation
from agent.llm import LLM


def build():
    cfg = Settings.from_env()
    return Business.load(), cfg, DB(cfg.db_path), LLM(cfg.llm_base_url, cfg.llm_model, cfg.llm_api_key)


def cmd_chat(_):
    biz, cfg, db, llm = build()
    conv = Conversation(biz, db, llm, f"cli-{uuid.uuid4().hex[:8]}", "+15550000000")
    print(f"[{'LLM: ' + cfg.llm_model if llm.enabled else 'offline rule engine'}]  Ctrl-D to hang up\n")
    print("Agent:", conv.greeting().say)
    while True:
        try:
            line = input("You:   ")
        except EOFError:
            break
        turn = conv.respond(line)
        print("Agent:", turn.say)
        if turn.end or turn.transfer:
            break
    rec = conv.finish()
    print(f"\n[call saved] outcome={rec['outcome']} summary={rec['summary']}")


def cmd_import(a):
    _, _, db, _ = build()
    print(f"imported {campaign.load_csv(db, a.csv)} contacts")


def cmd_campaign(a):
    biz, cfg, db, _ = build()
    rows = db._all("SELECT * FROM contacts ORDER BY id")
    for c in rows:
        ok, why = campaign.can_call(db, biz, c)
        print(f"{'CALL' if ok else 'SKIP'}  {c['phone']:<16} {c['name']:<20} {why}")
    if not a.dial:
        print("\n(dry run - add --dial to place calls)")
        return
    from agent.twilio_utils import place_call
    for c in campaign.eligible(db, biz):
        sid = place_call(cfg.twilio_sid, cfg.twilio_token, cfg.twilio_number, c["phone"],
                         f"{cfg.public_base_url}/voice/outbound?contact_id={c['id']}",
                         f"{cfg.public_base_url}/voice/status")
        print("dialled", c["phone"], sid)


def cmd_status(_):
    _, _, db, _ = build()
    for c in db.calls(10):
        print(f"#{c['id']} {c['direction']:<8} {c['phone']:<14} {c['outcome']}: {c['summary']}")
    print("\nAppointments:")
    for a in db.appointments():
        print(f"  {a['start']}  {a['name']}  {a['phone']}")
    print("Messages:")
    for m in db.messages():
        print(f"  {m['name']}: {m['body']}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chat").set_defaults(fn=cmd_chat)
    i = sub.add_parser("import"); i.add_argument("csv"); i.set_defaults(fn=cmd_import)
    c = sub.add_parser("campaign"); c.add_argument("--dial", action="store_true"); c.set_defaults(fn=cmd_campaign)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = p.parse_args()
    sys.exit(args.fn(args))
