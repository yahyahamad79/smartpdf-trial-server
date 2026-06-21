"""
Smart PDF — Trial & Privacy Server
-----------------------------------
A single small FastAPI service that provides:
  1. Trial management per device (POST /trial/check)
  2. Privacy policy page (GET /privacy)

The trial start date is stored ON THE SERVER (SQLite), keyed by the
device's Android ID. Reinstalling the app does NOT reset the trial,
because the server remembers the device.

Deploy on Render as a Web Service.
"""

import os
import sqlite3
from datetime import datetime, timezone
from contextlib import closing

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ------------------------------------------------------------------
# Configuration — change TRIAL_DAYS to whatever you want (e.g. 7, 5, 3)
# ------------------------------------------------------------------
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "7"))
DB_PATH = os.environ.get("DB_PATH", "trial.db")

app = FastAPI(title="Smart PDF Trial Server")


# ------------------------------------------------------------------
# Database helpers
# ------------------------------------------------------------------
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id   TEXT PRIMARY KEY,
                first_seen  TEXT NOT NULL
            )
            """
        )
        conn.commit()


init_db()


# ------------------------------------------------------------------
# Models
# ------------------------------------------------------------------
class TrialRequest(BaseModel):
    deviceId: str


class TrialResponse(BaseModel):
    status: str          # "active" | "expired"
    daysLeft: int        # remaining trial days (0 if expired)
    trialDays: int       # total trial length (for display)
    firstSeen: str       # ISO date the trial started


# ------------------------------------------------------------------
# Trial endpoint
# ------------------------------------------------------------------
@app.post("/trial/check", response_model=TrialResponse)
def trial_check(req: TrialRequest):
    device_id = (req.deviceId or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId is required")

    now = datetime.now(timezone.utc)

    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT first_seen FROM devices WHERE device_id = ?", (device_id,)
        )
        row = cur.fetchone()

        if row is None:
            # New device — start the trial now
            first_seen = now
            conn.execute(
                "INSERT INTO devices (device_id, first_seen) VALUES (?, ?)",
                (device_id, first_seen.isoformat()),
            )
            conn.commit()
        else:
            first_seen = datetime.fromisoformat(row[0])

    elapsed_days = (now - first_seen).days
    days_left = max(0, TRIAL_DAYS - elapsed_days)
    status = "active" if days_left > 0 else "expired"

    return TrialResponse(
        status=status,
        daysLeft=days_left,
        trialDays=TRIAL_DAYS,
        firstSeen=first_seen.date().isoformat(),
    )


# ------------------------------------------------------------------
# Health check
# ------------------------------------------------------------------
@app.get("/")
def root():
    return {"service": "Smart PDF Trial Server", "status": "ok", "trialDays": TRIAL_DAYS}


# ------------------------------------------------------------------
# Privacy policy page
# ------------------------------------------------------------------
@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    try:
        with open("privacy.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>Privacy policy not found</h1>"
