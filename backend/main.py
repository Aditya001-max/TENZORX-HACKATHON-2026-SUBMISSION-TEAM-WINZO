
from __future__ import annotations
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Set

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database as db
import vision
import risk_engine
from agent import LoanAgent

# ----------- app setup -----------

BASE_DIR = Path(__file__).parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"

app = FastAPI(title="Poonawalla Fincorp Loan Wizard",
              version="1.0.0",
              description="Agentic AI Video Call–Based Loan Onboarding")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db.init_db()
agent = LoanAgent()

# Per-session WebSocket connections
_ws_clients: Dict[str, Set[WebSocket]] = {}


async def _ws_broadcast(session_id: str, event: dict):
    dead = set()
    for ws in _ws_clients.get(session_id, set()):
        try:
            await ws.send_json(event)
        except Exception:
            dead.add(ws)
    for d in dead:
        _ws_clients.get(session_id, set()).discard(d)


# ----------- request models -----------

class GeoBody(BaseModel):
    latitude: float
    longitude: float
    city: str | None = None
    accuracy: float | None = None


class TranscriptBody(BaseModel):
    speaker: str           # 'customer' | 'agent'
    text: str
    intent: str | None = None


class TurnBody(BaseModel):
    customer_text: str
    current_slot: str | None = None
    collected: Dict[str, Any] = {}


class AgeBody(BaseModel):
    image_data_url: str


class ConsentBody(BaseModel):
    consent: bool


# ----------- routes -----------

@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.post("/api/session/start")
async def start_session(req: Request):
    sid = uuid.uuid4().hex[:12]
    ip = req.client.host if req.client else "unknown"
    ua = req.headers.get("user-agent", "unknown")
    db.create_session(sid, ip, ua)
    db.log_audit(sid, "SESSION_STARTED", {"ip": ip, "ua": ua})

    greeting = agent.greet()
    db.add_transcript(sid, "agent", greeting["agent_message"], intent="greeting")
    return {
        "session_id": sid,
        "greeting": greeting,
    }


@app.post("/api/session/{sid}/geo")
async def set_geo(sid: str, body: GeoBody):
    if not db.get_session(sid):
        raise HTTPException(404, "session not found")
    db.update_session(sid,
                      geo_lat=body.latitude,
                      geo_lng=body.longitude,
                      geo_city=body.city or "")
    db.log_audit(sid, "GEO_CAPTURED", body.model_dump())
    return {"ok": True}


@app.post("/api/session/{sid}/transcript")
async def add_transcript(sid: str, body: TranscriptBody):
    if not db.get_session(sid):
        raise HTTPException(404, "session not found")
    db.add_transcript(sid, body.speaker, body.text, body.intent)
    await _ws_broadcast(sid, {"type": "transcript", **body.model_dump()})
    return {"ok": True}


@app.post("/api/session/{sid}/turn")
async def conversational_turn(sid: str, body: TurnBody):
    """
    Heart of the agentic flow: takes the customer's spoken text,
    extracts data, decides the next question, and returns the
    agent's reply.
    """
    if not db.get_session(sid):
        raise HTTPException(404, "session not found")

    # Store customer utterance
    db.add_transcript(sid, "customer", body.customer_text)

    # Run the agent
    result = agent.step(body.customer_text,
                        body.current_slot or "full_name",
                        dict(body.collected))

    # Persist any newly extracted structured data
    if result.get("new_info"):
        clean = {k: v for k, v in result["new_info"].items() if k != "consent"}
        if clean:
            db.save_extracted_data(sid, clean)
        if "consent" in result["new_info"]:
            db.update_session(sid, consent_given=1 if result["new_info"]["consent"] else 0)

    db.add_transcript(sid, "agent", result["agent_message"],
                      intent=result.get("next_slot"))

    db.log_audit(sid, "TURN", {
        "customer": body.customer_text,
        "extracted": result.get("new_info"),
        "next_slot": result.get("next_slot"),
        "stage": result.get("stage"),
    })

    await _ws_broadcast(sid, {
        "type": "agent_turn",
        "agent_message": result["agent_message"],
        "extracted": result.get("new_info"),
        "next_slot": result.get("next_slot"),
        "completed": result.get("completed"),
    })

    return result


@app.post("/api/session/{sid}/age")
async def age_estimation(sid: str, body: AgeBody):
    if not db.get_session(sid):
        raise HTTPException(404, "session not found")

    age_result = vision.estimate_age(body.image_data_url)
    liveness = vision.liveness_check(body.image_data_url)

    if age_result.get("estimated_age"):
        db.save_extracted_data(sid, {"estimated_age": age_result["estimated_age"]})

    db.log_audit(sid, "AGE_ESTIMATION", {
        "age": age_result,
        "liveness": liveness,
    })

    await _ws_broadcast(sid, {
        "type": "age_estimation",
        "age": age_result,
        "liveness": liveness,
    })

    return {"age": age_result, "liveness": liveness}


@app.post("/api/session/{sid}/consent")
async def set_consent(sid: str, body: ConsentBody):
    if not db.get_session(sid):
        raise HTTPException(404, "session not found")
    db.update_session(sid, consent_given=1 if body.consent else 0)
    db.log_audit(sid, "CONSENT_SET", {"consent": body.consent})
    return {"ok": True, "consent": body.consent}


@app.post("/api/session/{sid}/finalize")
async def finalize(sid: str):
    """Run risk engine, generate offer, save everything, close session."""
    sess = db.get_session(sid)
    if not sess:
        raise HTTPException(404, "session not found")

    extracted = db.get_extracted_data(sid)
    extracted["phone"] = sess.get("phone")

    # Pull latest estimated age (already in extracted)
    estimated_age = extracted.get("estimated_age")

    assessment = risk_engine.assess(extracted, estimated_age)
    db.save_risk_assessment(sid, assessment)

    offer_payload = assessment["offer"]
    if offer_payload.get("eligible"):
        db.save_offer(sid, offer_payload)

    db.update_session(sid,
                      status="completed",
                      ended_at=datetime.utcnow().isoformat())

    closing = agent.closing_message(assessment["decision"])
    db.add_transcript(sid, "agent", closing, intent="closing")

    db.log_audit(sid, "FINALIZED", {
        "decision": assessment["decision"],
        "risk_score": assessment["risk_score"],
        "offer_eligible": offer_payload.get("eligible"),
    })

    payload = {
        "session_id": sid,
        "decision": assessment["decision"],
        "risk_score": assessment["risk_score"],
        "propensity_score": assessment["propensity_score"],
        "risk_band": assessment["risk_band"],
        "bureau_score": assessment["bureau_score"],
        "policy_failures": assessment["policy_failures"],
        "policy_warnings": assessment["policy_warnings"],
        "offer": offer_payload,
        "closing_message": closing,
        "extracted": extracted,
    }

    await _ws_broadcast(sid, {"type": "finalized", **payload})
    return payload


@app.get("/api/session/{sid}/report")
async def full_report(sid: str):
    if not db.get_session(sid):
        raise HTTPException(404, "session not found")
    return db.get_full_session_report(sid)


@app.get("/api/sessions")
async def list_sessions():
    """Admin endpoint to list all onboarding sessions."""
    return {"sessions": db.list_sessions(100)}


# ----------- WebSocket -----------

@app.websocket("/ws/session/{sid}")
async def session_ws(ws: WebSocket, sid: str):
    await ws.accept()
    _ws_clients.setdefault(sid, set()).add(ws)
    try:
        await ws.send_json({"type": "connected", "session_id": sid})
        while True:
            # We mostly push from server side; receive keeps the socket alive.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.get(sid, set()).discard(ws)


# ----------- static frontend (built React or static HTML) -----------

if FRONTEND_DIR.exists():
    # Mount only if frontend assets exist
    static_dir = FRONTEND_DIR
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def root():
        index = static_dir / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return JSONResponse({"message": "Loan Wizard API running. Frontend not built."})

    @app.get("/admin")
    async def admin_page():
        admin = static_dir / "admin.html"
        if admin.exists():
            return FileResponse(str(admin))
        return JSONResponse({"message": "Admin UI not available"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
