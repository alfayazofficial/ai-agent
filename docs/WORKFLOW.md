# Call workflow

## Inbound (client calls you)
```mermaid
flowchart TD
  A[Client dials your Twilio number] --> B[/voice/inbound/]
  B --> C[Greeting + AI disclosure]
  C --> D{Caller speaks - Twilio STT}
  D --> E{Compliance rules<br/>stop-calling / human?}
  E -- stop --> X[Add to DNC, hang up]
  E -- human --> T[Transfer, or take a message]
  E -- no --> F{LLM available?}
  F -- yes --> G[LLM returns say + action JSON]
  F -- no / error --> H[Offline rule engine]
  G --> I[Validated tools: book / take message]
  H --> I
  I --> J[Speak reply - loop to D]
  J -- goodbye / silence --> K[Hang up]
  K --> L[Save transcript + summary + outcome in SQLite]
  L --> M[POST to n8n webhook]
  M --> N[Calendar event / email owner / Sheets CRM row]
```

## Outbound (you call clients)
```mermaid
flowchart LR
  CSV[contacts.csv] --> IMP[cli.py import]
  IMP --> GATE{Consent? Not on DNC?<br/>Inside calling window?}
  GATE -- no --> SKIP[Skipped, reason shown]
  GATE -- yes --> DIAL[Twilio places call, 5s pacing]
  DIAL --> ANS{Answered}
  ANS --> INTRO[AI disclosure + purpose]
  INTRO --> OUT{Reply}
  OUT -- not interested --> DEC[outcome: declined]
  OUT -- bad time --> LAT[outcome: callback_later]
  OUT -- yes --> BOOK[Booking flow] --> LOG[Same post-call pipeline as inbound]
```

## Outcomes
`appointment_booked`, `message_taken`, `transferred`, `answered_questions`, `declined`, `callback_later`, `do_not_call`, `no_response`.

## n8n setup (free, self-hosted)
1. `docker compose up n8n` and open http://localhost:5678.
2. Import `workflows/n8n_post_call.json`; connect Google Calendar / Sheets / SMTP credentials; set your Sheet ID.
3. Activate it and set `N8N_WEBHOOK_URL=http://n8n:5678/webhook/voice-agent-call` in `.env`.

Payload sent by the agent: `{event, call{id,call_sid,direction,phone,started,ended,transcript,summary,outcome}, appointments[], messages[]}`.
