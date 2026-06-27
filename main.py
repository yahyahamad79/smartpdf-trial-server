"""
Smart PDF — Trial & Privacy Server
-----------------------------------
A single small FastAPI service that provides:
  1. Trial management per device (POST /trial/check)
  2. Privacy policy page (GET /privacy)
  3. PDF page rendering for preview (POST /render-page)  <-- NEW

The trial start date is stored ON THE SERVER (SQLite), keyed by the
device's Android ID. Reinstalling the app does NOT reset the trial,
because the server remembers the device.

The /render-page endpoint renders a SINGLE PDF page to a PNG image,
so the mobile app can show a real preview WITHOUT any native library
(it just displays a normal <Image>). This keeps the app fully offline
by default; preview is an online-only extra.

Deploy on Render as a Web Service.
"""

import os
import io
import sqlite3
from datetime import datetime, timezone
from contextlib import closing

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ------------------------------------------------------------------
# Configuration — change TRIAL_DAYS to whatever you want (e.g. 7, 5, 3)
# ------------------------------------------------------------------
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "7"))
DB_PATH = os.environ.get("DB_PATH", "trial.db")

# Max upload size for rendering (MB) — protects the free server
MAX_RENDER_MB = int(os.environ.get("MAX_RENDER_MB", "25"))
# Render scale (higher = sharper but heavier). 2.0 is a good preview balance.
RENDER_ZOOM = float(os.environ.get("RENDER_ZOOM", "2.0"))

# Session store for "upload once" — keeps the PDF bytes in memory keyed by
# a session id, so the app uploads the file ONCE and then requests pages by id
# (no re-upload per page). Sessions expire after SESSION_TTL seconds.
import uuid
import time as _time
SESSION_TTL = int(os.environ.get("SESSION_TTL", "1800"))  # 30 minutes
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "40"))   # cap memory use
_sessions = {}  # sid -> {"data": bytes, "total": int, "ts": float}

def _purge_sessions():
    now = _time.time()
    expired = [k for k, v in _sessions.items() if now - v["ts"] > SESSION_TTL]
    for k in expired:
        _sessions.pop(k, None)
    # if still too many, drop oldest
    if len(_sessions) > MAX_SESSIONS:
        oldest = sorted(_sessions.items(), key=lambda kv: kv[1]["ts"])
        for k, _ in oldest[: len(_sessions) - MAX_SESSIONS]:
            _sessions.pop(k, None)

app = FastAPI(title="Smart PDF Trial Server")

# ------------------------------------------------------------------
# CORS — allow the mobile app to call the render endpoint
# ------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # mobile app has no fixed origin
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
# PDF page rendering endpoint (for preview) — NEW
# ------------------------------------------------------------------
@app.post("/render-page")
async def render_page(
    file: UploadFile = File(...),
    page: int = Form(0),          # 0-based page index
    zoom: float = Form(None),     # optional override
):
    """
    Render ONE page of an uploaded PDF to a PNG image.
    Returns the PNG bytes directly (image/png).
    The uploaded file is processed in memory and never stored on disk.
    """
    # Lazy import so the server still boots even if PyMuPDF is missing
    try:
        import fitz  # PyMuPDF
    except Exception:
        raise HTTPException(status_code=500, detail="Renderer not available on server")

    # Read upload with a size guard
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_RENDER_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File too large (>{MAX_RENDER_MB}MB)")

    use_zoom = zoom if (zoom and zoom > 0) else RENDER_ZOOM
    # clamp zoom to a sane range
    use_zoom = max(1.0, min(use_zoom, 3.0))

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid PDF")

    try:
        total = doc.page_count
        if total == 0:
            raise HTTPException(status_code=400, detail="PDF has no pages")
        # clamp page index
        idx = max(0, min(page, total - 1))
        pdf_page = doc.load_page(idx)
        matrix = fitz.Matrix(use_zoom, use_zoom)
        pix = pdf_page.get_pixmap(matrix=matrix, alpha=False)
        png_bytes = pix.tobytes("png")
    finally:
        doc.close()

    return StreamingResponse(
        io.BytesIO(png_bytes),
        media_type="image/png",
        headers={"X-Total-Pages": str(total)},
    )



# ------------------------------------------------------------------
# Upload-once session endpoints — NEW (efficient for large PDFs)
# ------------------------------------------------------------------
@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    """
    Upload a PDF ONCE. Returns a sessionId + total page count.
    The app then calls /render/{sid}/{page} per page WITHOUT re-uploading.
    Stored in memory only, auto-expires.
    """
    try:
        import fitz
    except Exception:
        raise HTTPException(status_code=500, detail="Renderer not available on server")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_RENDER_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File too large (>{MAX_RENDER_MB}MB)")

    try:
        doc = fitz.open(stream=data, filetype="pdf")
        total = doc.page_count
        doc.close()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid PDF")
    if total == 0:
        raise HTTPException(status_code=400, detail="PDF has no pages")

    _purge_sessions()
    sid = uuid.uuid4().hex
    _sessions[sid] = {"data": data, "total": total, "ts": _time.time()}
    return {"sessionId": sid, "totalPages": total}


@app.get("/render/{sid}/{page}")
def render_by_session(sid: str, page: int, zoom: float = None):
    """
    Render ONE page from a previously uploaded session (by sessionId).
    No re-upload needed. page is 1-based here for convenience.
    """
    try:
        import fitz
    except Exception:
        raise HTTPException(status_code=500, detail="Renderer not available on server")

    sess = _sessions.get(sid)
    if not sess:
        # 410 Gone tells the app to re-upload
        raise HTTPException(status_code=410, detail="Session expired")
    sess["ts"] = _time.time()  # refresh TTL on use

    use_zoom = zoom if (zoom and zoom > 0) else RENDER_ZOOM
    use_zoom = max(1.0, min(use_zoom, 3.0))

    try:
        doc = fitz.open(stream=sess["data"], filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid PDF")
    try:
        total = doc.page_count
        idx = max(0, min(page - 1, total - 1))  # page is 1-based
        pdf_page = doc.load_page(idx)
        matrix = fitz.Matrix(use_zoom, use_zoom)
        pix = pdf_page.get_pixmap(matrix=matrix, alpha=False)
        png_bytes = pix.tobytes("png")
    finally:
        doc.close()

    return StreamingResponse(
        io.BytesIO(png_bytes),
        media_type="image/png",
        headers={"X-Total-Pages": str(total)},
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
