import json
import logging
import re

import httpx

log = logging.getLogger("agent.llm")


class LLM:
    """OpenAI-compatible chat client: works with Ollama, Groq free tier, LM Studio, vLLM..."""

    def __init__(self, base_url: str = "", model: str = "llama3.2", api_key: str = "none", timeout: float = 12.0):
        self.base_url, self.model, self.api_key, self.timeout = base_url.rstrip("/"), model, api_key, timeout

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def chat(self, messages: list[dict], temperature: float = 0.3) -> str | None:
        if not self.enabled:
            return None
        try:
            r = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": 300},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:  # network, auth, bad payload: caller falls back to rules
            log.warning("LLM call failed: %s", exc)
            return None


def extract_json(text: str | None) -> dict | None:
    """Small models wrap JSON in prose or code fences; pull out the first object."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
