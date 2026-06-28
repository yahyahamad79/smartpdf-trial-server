"""
Smart PDF — Trial, Privacy, Rendering & Compression Server
-----------------------------------------------------------
FastAPI service providing:
  1. Trial management per device (POST /trial/check)
  2. Privacy policy page (GET /privacy)
  3. PDF page rendering for preview (POST /render-page, /upload, /render/{sid}/{page})
  4. PDF compression (POST /compress)

Deploy on Render as a Web Service.
"""

import os
import io
import uuid
import time as _time
import sqlite3
from datetime import datetime, timezone
from contextlib import closing

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "7"))
DB_PATH = os.environ.get("DB_PATH", "trial.db")
MAX_RENDER_MB = int(os.environ.get("MAX_RENDER_MB", "25"))
RENDER_ZOOM = float(os.environ.get("RENDER_ZOOM", "2.0"))

SESSION_TTL = int(os.environ.get("SESSION_TTL", "1800"))   # 30 min
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "40"))
_sessions = {}  # sid -> {"data": bytes, "total": int, "ts": float}

app = FastAPI(title="Smart PDF Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Database
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


def _purge_sessions():
    now = _time.time()
    for k in [k for k, v in _sessions.items() if now - v["ts"] > SESSION_TTL]:
        _sessions.pop(k, None)
    if len(_sessions) > MAX_SESSIONS:
        oldest = sorted(_sessions.items(), key=lambda kv: kv[1]["ts"])
        for k, _ in oldest[: len(_sessions) - MAX_SESSIONS]:
            _sessions.pop(k, None)


# ------------------------------------------------------------------
# Models
# ------------------------------------------------------------------
class TrialRequest(BaseModel):
    deviceId: str


class TrialResponse(BaseModel):
    status: str
    daysLeft: int
    trialDays: int
    firstSeen: str


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
        cur = conn.execute("SELECT first_seen FROM devices WHERE device_id = ?", (device_id,))
        row = cur.fetchone()
        if row is None:
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
        status=status, daysLeft=days_left, trialDays=TRIAL_DAYS,
        firstSeen=first_seen.date().isoformat(),
    )


# ------------------------------------------------------------------
# Render a single page from a direct upload (legacy, still supported)
# ------------------------------------------------------------------
@app.post("/render-page")
async def render_page(file: UploadFile = File(...), page: int = Form(0), zoom: float = Form(None)):
    try:
        import fitz
    except Exception:
        raise HTTPException(status_code=500, detail="Renderer not available")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_RENDER_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File too large (>{MAX_RENDER_MB}MB)")

    use_zoom = max(1.0, min(zoom if (zoom and zoom > 0) else RENDER_ZOOM, 3.0))
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid PDF")
    try:
        total = doc.page_count
        if total == 0:
            raise HTTPException(status_code=400, detail="PDF has no pages")
        idx = max(0, min(page, total - 1))
        pix = doc.load_page(idx).get_pixmap(matrix=fitz.Matrix(use_zoom, use_zoom), alpha=False)
        png = pix.tobytes("png")
    finally:
        doc.close()
    return StreamingResponse(io.BytesIO(png), media_type="image/png", headers={"X-Total-Pages": str(total)})


# ------------------------------------------------------------------
# Upload once -> sessionId, then render pages by id
# ------------------------------------------------------------------
@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    try:
        import fitz
    except Exception:
        raise HTTPException(status_code=500, detail="Renderer not available")

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
    try:
        import fitz
    except Exception:
        raise HTTPException(status_code=500, detail="Renderer not available")

    sess = _sessions.get(sid)
    if not sess:
        raise HTTPException(status_code=410, detail="Session expired")
    sess["ts"] = _time.time()

    use_zoom = max(1.0, min(zoom if (zoom and zoom > 0) else RENDER_ZOOM, 3.0))
    try:
        doc = fitz.open(stream=sess["data"], filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid PDF")
    try:
        total = doc.page_count
        idx = max(0, min(page - 1, total - 1))  # 1-based
        pix = doc.load_page(idx).get_pixmap(matrix=fitz.Matrix(use_zoom, use_zoom), alpha=False)
        png = pix.tobytes("png")
    finally:
        doc.close()
    return StreamingResponse(io.BytesIO(png), media_type="image/png", headers={"X-Total-Pages": str(total)})


# ------------------------------------------------------------------
# Compress a PDF (re-encode embedded images smaller)
# ------------------------------------------------------------------
@app.post("/compress")
async def compress_pdf(file: UploadFile = File(...), level: str = Form("medium")):
    try:
        import fitz
    except Exception:
        raise HTTPException(status_code=500, detail="Compressor not available")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_RENDER_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File too large (>{MAX_RENDER_MB}MB)")

    presets = {
        "low":    {"quality": 75, "max_dim": 2000},
        "medium": {"quality": 55, "max_dim": 1500},
        "high":   {"quality": 38, "max_dim": 1100},
    }
    cfg = presets.get(level, presets["medium"])

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid PDF")

    try:
        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            for img in page.get_images(full=True):
                xref = img[0]
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.width < 50 or pix.height < 50:
                        pix = None
                        continue
                    if pix.n >= 5 or pix.alpha:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    max_dim = cfg["max_dim"]
                    if max(pix.width, pix.height) > max_dim:
                        scale = max_dim / max(pix.width, pix.height)
                        new_w = max(1, int(pix.width * scale))
                        new_h = max(1, int(pix.height * scale))
                        pix = _resize_pix(fitz, pix, new_w, new_h)
                    new_bytes = pix.tobytes("jpeg", jpg_quality=cfg["quality"])
                    doc.update_stream(xref, new_bytes, new=True)
                    doc.xref_set_key(xref, "Filter", "/DCTDecode")
                    doc.xref_set_key(xref, "ColorSpace", "/DeviceRGB")
                    doc.xref_set_key(xref, "BitsPerComponent", "8")
                    doc.xref_set_key(xref, "Width", str(pix.width))
                    doc.xref_set_key(xref, "Height", str(pix.height))
                    pix = None
                except Exception:
                    continue
        out = doc.tobytes(garbage=4, deflate=True, clean=True)
    finally:
        doc.close()

    final = out if len(out) < len(data) else data
    return StreamingResponse(
        io.BytesIO(final),
        media_type="application/pdf",
        headers={
            "X-Original-KB": str(len(data) // 1024),
            "X-Compressed-KB": str(len(final) // 1024),
            "Content-Disposition": "attachment; filename=compressed.pdf",
        },
    )


def _resize_pix(fitz, pix, new_w, new_h):
    """تصغير pixmap عبر insert_image في صفحة بحجم جديد (آمن)."""
    try:
        src_png = pix.tobytes("png")
        tmp = fitz.open()
        pg = tmp.new_page(width=new_w, height=new_h)
        pg.insert_image(fitz.Rect(0, 0, new_w, new_h), stream=src_png)
        result = pg.get_pixmap(matrix=fitz.Identity, alpha=False)
        tmp.close()
        return result
    except Exception:
        return pix


# ------------------------------------------------------------------
# Health check
# ------------------------------------------------------------------
@app.get("/")
def root():
    return {"service": "Smart PDF Server", "status": "ok", "trialDays": TRIAL_DAYS}


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
