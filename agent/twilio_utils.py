import base64
import hashlib
import hmac
from xml.sax.saxutils import escape

import httpx


def valid_signature(auth_token: str, url: str, params: dict, signature: str) -> bool:
    """Twilio request validation: HMAC-SHA1(url + sorted key/value pairs)."""
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


def twiml_say_gather(text: str, language: str, action: str = "/voice/gather") -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Gather input="speech" action="{action}" method="POST" speechTimeout="auto" language="{language}">'
            f'<Say language="{language}">{escape(text)}</Say></Gather>'
            f'<Redirect method="POST">{action}</Redirect></Response>')


def twiml_say_hangup(text: str, language: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Say language="{language}">{escape(text)}</Say><Hangup/></Response>')


def twiml_say_dial(text: str, number: str, language: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Say language="{language}">{escape(text)}</Say><Dial>{escape(number)}</Dial></Response>')


def place_call(sid: str, token: str, from_number: str, to_number: str, answer_url: str, status_url: str) -> str:
    r = httpx.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
        auth=(sid, token),
        data={"From": from_number, "To": to_number, "Url": answer_url, "StatusCallback": status_url,
              "StatusCallbackEvent": "completed"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["sid"]
