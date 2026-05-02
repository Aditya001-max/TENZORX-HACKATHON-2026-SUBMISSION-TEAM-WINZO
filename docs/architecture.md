# Architecture

## Component overview

```
┌────────────────── BROWSER (customer) ───────────────────┐
│                                                          │
│   Landing → Consent → Video Call → Result               │
│                                                          │
│   • getUserMedia() ── camera + mic streams              │
│   • SpeechRecognition ── push-to-talk STT (en-IN)       │
│   • SpeechSynthesis ── Maya speaks back                 │
│   • Geolocation ── city/lat/lng (with consent)          │
│   • Canvas snapshot every 6s ── for age estimation      │
│   • WebSocket ── live event stream                      │
└──────────────────────────┬───────────────────────────────┘
                           │  REST + WS
┌──────────────────────────▼───────────────────────────────┐
│                  FASTAPI BACKEND                         │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Conversational Layer                            │   │
│  │  agent.py — LoanAgent state machine              │   │
│  │  Slots: name → age → employment → income →       │   │
│  │         purpose → amount → PAN → consent         │   │
│  └────────────────┬─────────────────────────────────┘   │
│                   │                                      │
│  ┌────────────────▼──────────────┐  ┌─────────────────┐ │
│  │  NLU                          │  │  Vision         │ │
│  │  nlu.py — rule extraction     │  │  vision.py      │ │
│  │  detect_employment, _amount,  │  │  estimate_age   │ │
│  │  _pan, _consent, etc.         │  │  liveness_check │ │
│  └────────────────┬──────────────┘  └────────┬────────┘ │
│                   │                          │          │
│  ┌────────────────▼──────────────────────────▼───────┐  │
│  │  Risk + Policy Engine (deterministic)            │  │
│  │  risk_engine.py                                  │  │
│  │  • fetch_bureau_score (mocked, deterministic)    │  │
│  │  • evaluate_policy → hard_failures, warnings     │  │
│  │  • compute_risk_score, propensity, band          │  │
│  │  • generate_offer → 5 tenure options + EMI       │  │
│  └─────────────────────────────┬────────────────────┘  │
│                                │                         │
│  ┌─────────────────────────────▼───────────────────┐    │
│  │  Audit Repository (SQLite)                      │    │
│  │  database.py                                    │    │
│  │  Tables: sessions, transcripts, extracted_data, │    │
│  │  risk_assessments, offers, audit_log            │    │
│  └─────────────────────────────────────────────────┘    │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

## Conversation flow

```
        ┌─────────┐
        │  start  │  POST /api/session/start
        └────┬────┘
             ▼
   ┌──────────────────┐
   │  Maya greets     │  greeting + first slot = full_name
   └────────┬─────────┘
            ▼
   ┌────────────────────────────────────────┐
   │  customer speaks  ──►  /api/.../turn   │
   │  agent.step(text, current_slot, data)  │◄─┐
   │  → nlu.extract_all(text, context)      │  │ loop until
   │  → next_slot computed                  │  │ all slots filled
   │  → Maya replies + advances             │──┘
   └────────────────────┬───────────────────┘
                        ▼
   ┌────────────────────────────────────────┐
   │  All slots filled → completed = true   │
   └────────────────────┬───────────────────┘
                        ▼
   ┌────────────────────────────────────────┐
   │  POST /api/.../finalize                │
   │  risk_engine.assess(profile)           │
   │  → hard_failures? → REJECT             │
   │  → warnings only? → REFER              │
   │  → clean? → APPROVE + offer            │
   └────────────────────┬───────────────────┘
                        ▼
   ┌────────────────────────────────────────┐
   │  Result screen + audit row written     │
   └────────────────────────────────────────┘
```

## Why the agent never overrides the rules

The problem statement is explicit: the LLM/agent layer must remain **advisory**. We enforce this structurally — the agent module knows nothing about policy thresholds. Its only job is to extract slots and converse. The `risk_engine` is the *only* code path that produces a decision, and `main.py` calls it directly with the captured profile. There is no API surface through which the agent can influence the decision after the fact.

## Data model

```
sessions                         transcripts
─────────                        ───────────
id (TEXT, pk)                    session_id (FK)
created_at                       speaker (agent|customer)
status                           text
geo_lat, geo_lng, geo_city       timestamp
ip_address
extracted_json                   risk_assessments
                                 ────────────────
extracted_data                   session_id (FK)
──────────────                   risk_score
session_id (FK)                  bureau_score
field, value, source             risk_band
                                 hard_failures (json)
offers                           warnings (json)
──────                           policy_decision
session_id (FK)
loan_amount, tenure_months,      audit_log
interest_rate, emi               ─────────
options_json                     session_id (FK)
                                 event_type
                                 payload (json)
                                 timestamp
```

## Extending to production

| Concern | Prototype | Production direction |
|---|---|---|
| STT | Browser Web Speech API | Cloud STT (Azure / Google / Deepgram) for accuracy + diarisation |
| TTS | Browser SpeechSynthesis | Neural TTS with Indic accent voices |
| Age estimation | OpenCV + heuristics | Trained CNN (e.g. DEX / SSR-Net) on age regression |
| Bureau | Deterministic mock | Live CIBIL / Experian / Equifax integration |
| Liveness | HSV variance check | Active challenges (blink, head turn) + spoofing model |
| Storage | SQLite | PostgreSQL + S3 for video / audio artifacts |
| Compliance | In-app verbal consent + audit log | Signed consent + DPDP-compliant retention policies |
| LLM layer | Rule-based deterministic | LLM with tool-use, **still gated by `risk_engine`** |
