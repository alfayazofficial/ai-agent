import logging

import httpx

log = logging.getLogger("agent.notify")


def post_call_event(webhook_url: str, call: dict, appointments: list, messages: list) -> None:
    """Fire-and-forget POST to n8n/Zapier/Make so the workflow can email, log to Sheets, create events..."""
    if not webhook_url or not call:
        return
    payload = {"event": "call.completed", "call": call, "appointments": appointments, "messages": messages}
    try:
        httpx.post(webhook_url, json=payload, timeout=8)
    except Exception as exc:
        log.warning("post-call webhook failed: %s", exc)
