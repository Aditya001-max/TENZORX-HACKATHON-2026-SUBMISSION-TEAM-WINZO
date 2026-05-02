
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

DB_PATH = Path(__file__).parent / "loan_wizard.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize all required tables."""
    conn = get_conn()
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        customer_name TEXT,
        phone TEXT,
        email TEXT,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        status TEXT DEFAULT 'active',
        ip_address TEXT,
        device_info TEXT,
        geo_lat REAL,
        geo_lng REAL,
        geo_city TEXT,
        consent_given INTEGER DEFAULT 0,
        video_path TEXT
    );

    CREATE TABLE IF NOT EXISTS transcripts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        speaker TEXT NOT NULL,        -- 'agent' or 'customer'
        text TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        intent TEXT,                  -- extracted intent (employment, income, etc.)
        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
    );

    CREATE TABLE IF NOT EXISTS extracted_data (
        session_id TEXT PRIMARY KEY,
        full_name TEXT,
        declared_age INTEGER,
        estimated_age INTEGER,
        employment_type TEXT,
        employer_name TEXT,
        monthly_income REAL,
        loan_purpose TEXT,
        loan_amount_requested REAL,
        existing_emi REAL,
        pan TEXT,
        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
    );

    CREATE TABLE IF NOT EXISTS risk_assessments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        risk_score INTEGER,
        propensity_score INTEGER,
        risk_band TEXT,
        bureau_score INTEGER,
        decision TEXT,                -- approve / reject / refer
        reasons TEXT,                 -- JSON list of reason codes
        created_at TEXT NOT NULL,
        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
    );

    CREATE TABLE IF NOT EXISTS offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        loan_amount REAL,
        tenure_months INTEGER,
        interest_rate REAL,
        emi REAL,
        offer_json TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        event_type TEXT NOT NULL,
        payload TEXT,
        timestamp TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()


# ---------- session helpers ----------

def create_session(session_id: str, ip: str, device: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO sessions (session_id, started_at, ip_address, device_info) VALUES (?,?,?,?)",
        (session_id, datetime.utcnow().isoformat(), ip, device),
    )
    conn.commit()
    conn.close()


def update_session(session_id: str, **fields) -> None:
    if not fields:
        return
    conn = get_conn()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE sessions SET {cols} WHERE session_id=?",
                 (*fields.values(), session_id))
    conn.commit()
    conn.close()


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------- transcript ----------

def add_transcript(session_id: str, speaker: str, text: str, intent: Optional[str] = None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO transcripts (session_id, speaker, text, timestamp, intent) VALUES (?,?,?,?,?)",
        (session_id, speaker, text, datetime.utcnow().isoformat(), intent),
    )
    conn.commit()
    conn.close()


def get_transcripts(session_id: str) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT speaker, text, timestamp, intent FROM transcripts WHERE session_id=? ORDER BY id ASC",
        (session_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- extracted data ----------

def save_extracted_data(session_id: str, data: Dict[str, Any]) -> None:
    conn = get_conn()
    existing = conn.execute("SELECT session_id FROM extracted_data WHERE session_id=?", (session_id,)).fetchone()
    fields = ["full_name", "declared_age", "estimated_age", "employment_type",
              "employer_name", "monthly_income", "loan_purpose",
              "loan_amount_requested", "existing_emi", "pan"]
    values = [data.get(f) for f in fields]
    if existing:
        cols = ", ".join(f"{f}=COALESCE(?, {f})" for f in fields)
        conn.execute(f"UPDATE extracted_data SET {cols} WHERE session_id=?", (*values, session_id))
    else:
        cols = ", ".join(["session_id"] + fields)
        placeholders = ",".join(["?"] * (len(fields) + 1))
        conn.execute(f"INSERT INTO extracted_data ({cols}) VALUES ({placeholders})", (session_id, *values))
    conn.commit()
    conn.close()


def get_extracted_data(session_id: str) -> Dict[str, Any]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM extracted_data WHERE session_id=?", (session_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


# ---------- risk + offer ----------

def save_risk_assessment(session_id: str, assessment: Dict[str, Any]) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO risk_assessments
           (session_id, risk_score, propensity_score, risk_band, bureau_score,
            decision, reasons, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (session_id,
         assessment.get("risk_score"),
         assessment.get("propensity_score"),
         assessment.get("risk_band"),
         assessment.get("bureau_score"),
         assessment.get("decision"),
         json.dumps(assessment.get("reasons", [])),
         datetime.utcnow().isoformat())
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def save_offer(session_id: str, offer: Dict[str, Any]) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO offers (session_id, loan_amount, tenure_months,
           interest_rate, emi, offer_json, created_at) VALUES (?,?,?,?,?,?,?)""",
        (session_id,
         offer.get("loan_amount"),
         offer.get("tenure_months"),
         offer.get("interest_rate"),
         offer.get("emi"),
         json.dumps(offer),
         datetime.utcnow().isoformat())
    )
    oid = cur.lastrowid
    conn.commit()
    conn.close()
    return oid


# ---------- audit ----------

def log_audit(session_id: Optional[str], event_type: str, payload: Dict[str, Any]) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO audit_log (session_id, event_type, payload, timestamp) VALUES (?,?,?,?)",
        (session_id, event_type, json.dumps(payload), datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def get_full_session_report(session_id: str) -> Dict[str, Any]:
    """Compose a full audit report for a session."""
    return {
        "session": get_session(session_id),
        "transcripts": get_transcripts(session_id),
        "extracted_data": get_extracted_data(session_id),
        "risk": _latest_risk(session_id),
        "offer": _latest_offer(session_id),
    }


def _latest_risk(session_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM risk_assessments WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (session_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["reasons"] = json.loads(d.get("reasons") or "[]")
    except Exception:
        d["reasons"] = []
    return d


def _latest_offer(session_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM offers WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (session_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["offer_json"] = json.loads(d.get("offer_json") or "{}")
    except Exception:
        d["offer_json"] = {}
    return d


def list_sessions(limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
