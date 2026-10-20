# AI Voice Agent - answers and makes client calls, on free tools

An AI phone agent that **takes inbound calls** (answers questions, books appointments, takes messages, transfers to a human) and **makes outbound calls** (reminders / follow-ups with consent, DNC and calling-hours checks). Every call is transcribed, summarised, stored, and pushed into an automation workflow.

## The free stack

| Layer | Free option used | Notes |
|---|---|---|
| Brain (LLM) | **Ollama** local (`llama3.2`) or **Groq free tier** | Any OpenAI-compatible endpoint. No LLM? The **offline rule engine** still runs the call. |
| Phone line | **Twilio trial** credit (STT + TTS included via `<Gather>`/`<Say>`) | Trial can only call verified numbers and plays a trial message. |
| 100% free test | **Browser demo** `/demo` (Web Speech API) and `python cli.py chat` | No phone number or account needed. |
| Backend / DB | FastAPI + SQLite | Single file DB in `data/`. |
| Workflow | **n8n** self-hosted (`workflows/n8n_post_call.json`) | Calendar event, email, Google Sheets CRM row. |
| Public URL | **Cloudflare quick tunnel** | Free, for Twilio webhooks. |

Honest limit: real PSTN phone calls are never fully free - Twilio gives trial credit, and Telnyx/Plivo offer similar. Everything else here costs nothing.

## Quick start (2 minutes, no accounts)
```bash
pip install -r requirements.txt
python cli.py chat                  # talk to it in the terminal (offline engine)
uvicorn agent.server:get_app --factory --port 8000   # then open http://localhost:8000/demo
python -m pytest -q
```
Optional smarter brain: `ollama pull llama3.2`, copy `.env.example` to `.env`, then `set -a; source .env; set +a`.

## Real phone calls
1. Create a free Twilio account, buy/claim the trial number, fill `.env`.
2. `cloudflared tunnel --url http://localhost:8000` -> put the URL in `PUBLIC_BASE_URL`.
3. In Twilio, set the number's *Voice -> A call comes in* webhook to `POST {PUBLIC_BASE_URL}/voice/inbound` and the status callback to `/voice/status`.
4. Call your number. Requests are verified with Twilio's signature when `TWILIO_AUTH_TOKEN` is set.

## Outbound campaigns
```bash
python cli.py import data/contacts.example.csv   # columns: name,phone,consent,notes
python cli.py campaign                           # DRY RUN: shows who would be called and why others are skipped
python cli.py campaign --dial                    # places the calls
```
Or one call via `POST /api/outbound` with header `X-API-Key`. Contacts without recorded consent, on the DNC list, or outside `calling_window` are **never dialled**.

## Make it your business
Edit `config/business.yaml` (name, hours, services, FAQs, transfer number, outbound purpose). No code changes.

## Everything in one command
`docker compose up` starts the agent, Ollama, n8n and a tunnel. See [docs/WORKFLOW.md](docs/WORKFLOW.md) for the call flows and n8n setup.

## Layout
```
agent/engine.py     call brain (compliance rules -> LLM -> offline fallback), validated booking
agent/server.py     Twilio webhooks, browser demo, admin API
agent/campaign.py   outbound gating (consent / DNC / hours)
agent/db.py         SQLite: calls, appointments, messages, contacts, dnc
workflows/          n8n post-call automation
tests/              14 tests covering flows, double-booking, signatures, compliance
```

## Safety and legal notes
- The agent always **identifies itself as an AI** and honours "stop calling" immediately (DNC), outside LLM control.
- The LLM can only *request* a booking; the system validates hours, past dates and double-booking before confirming.
- You are responsible for local rules (TCPA, GDPR, call-recording consent, DNC registries). Only call people who gave consent. This project does not record audio.
