import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

import yaml

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_hhmm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def parse_range(s: str) -> tuple[time, time]:
    a, b = s.split("-")
    return parse_hhmm(a), parse_hhmm(b)


@dataclass
class Business:
    name: str = "Our Company"
    agent_name: str = "Ava"
    timezone: str = "UTC"
    transfer_number: str = ""
    address: str = ""
    slot_minutes: int = 30
    hours: dict = field(default_factory=dict)
    services: list = field(default_factory=list)
    faqs: list = field(default_factory=list)
    outbound: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None = None) -> "Business":
        path = path or os.getenv("BUSINESS_CONFIG", "config/business.yaml")
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def day_hours(self, weekday: int) -> tuple[time, time] | None:
        raw = self.hours.get(DAYS[weekday])
        return parse_range(raw) if raw else None

    def hours_sentence(self) -> str:
        parts = []
        for i, d in enumerate(DAYS):
            if self.hours.get(d):
                o, c = parse_range(self.hours[d])
                parts.append(f"{d.capitalize()} {o.strftime('%H:%M')} to {c.strftime('%H:%M')}")
        return ", ".join(parts) or "by appointment"


@dataclass
class Settings:
    llm_base_url: str = ""
    llm_model: str = "llama3.2"
    llm_api_key: str = "none"
    db_path: str = "data/agent.db"
    api_key: str = ""
    twilio_sid: str = ""
    twilio_token: str = ""
    twilio_number: str = ""
    public_base_url: str = ""
    speech_language: str = "en-US"
    n8n_webhook_url: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.getenv
        return cls(
            llm_base_url=e("LLM_BASE_URL", ""),
            llm_model=e("LLM_MODEL", "llama3.2"),
            llm_api_key=e("LLM_API_KEY", "none"),
            db_path=e("DB_PATH", "data/agent.db"),
            api_key=e("API_KEY", ""),
            twilio_sid=e("TWILIO_ACCOUNT_SID", ""),
            twilio_token=e("TWILIO_AUTH_TOKEN", ""),
            twilio_number=e("TWILIO_PHONE_NUMBER", ""),
            public_base_url=e("PUBLIC_BASE_URL", "").rstrip("/"),
            speech_language=e("SPEECH_LANGUAGE", "en-US"),
            n8n_webhook_url=e("N8N_WEBHOOK_URL", ""),
        )
