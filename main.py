# ════════════════════════════════════════════════════════════════
# Smart PDF — Trial & Tools Server  (main.py)
# FastAPI + PyMuPDF  |  Deployed on Render
#
# Endpoints:
#   POST /trial/check          — تحقق/تسجيل تجربة الجهاز
#   GET  /                      — health check (يُستخدم أيضاً لإيقاظ السيرفر)
#   GET  /privacy               — سياسة الخصوصية
#   POST /upload                — رفع PDF لجلسة معاينة (TTL 30 دقيقة)
#   GET  /render/{sid}/{page}   — صورة صفحة (1-based) PNG
#   POST /render-page           — (legacy) معاينة صفحة واحدة
#   POST /compress              — ضغط PDF (مُصلح: المستويات تعمل فعلاً)
# ════════════════════════════════════════════════════════════════

import io
import time
import sqlite3
import threading
from datetime import datetime, timezone

import fitz  # PyMuPDF
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Path
from fastapi.responses import Response, JSONResponse, HTMLResponse, PlainTextResponse

app = FastAPI(title="Smart PDF Server")

# ─────────────────────────────────────────────────────────────
# الإعدادات
# ─────────────────────────────────────────────────────────────
TRIAL_DAYS      = 7
DB_PATH         = "trial.db"
MAX_UPLOAD_MB   = 60          # حد رفع المعاينة
MAX_COMPRESS_MB = 100         # حد رفع الضغط (أكبر — الملفات الكبيرة)
SESSION_TTL     = 30 * 60     # 30 دقيقة
MAX_SESSIONS    = 40

# ─────────────────────────────────────────────────────────────
# قاعدة بيانات التجربة (SQLite)
# ─────────────────────────────────────────────────────────────
_db_lock = threading.Lock()

def _init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS trials (
                device_id  TEXT PRIMARY KEY,
                started_at TEXT NOT NULL
            )
        """)
        c.commit()

_init_db()

@app.post("/trial/check")
async def trial_check(device_id: str = Form(...)):
    """يُسجّل بداية التجربة عند أول مرة، ويُرجع الأيام المتبقية."""
    if not device_id or len(device_id) < 4:
        raise HTTPException(status_code=400, detail="Invalid device id")

    now = datetime.now(timezone.utc)
    with _db_lock, sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT started_at FROM trials WHERE device_id=?", (device_id,)
        ).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO trials(device_id, started_at) VALUES(?,?)",
                (device_id, now.isoformat()),
            )
            c.commit()
            started = now
        else:
            started = datetime.fromisoformat(row[0])

    elapsed_days = (now - started).total_seconds() / 86400.0
    remaining = max(0, TRIAL_DAYS - int(elapsed_days))
    return {
        "device_id": device_id,
        "trial_days": TRIAL_DAYS,
        "days_remaining": remaining,
        "expired": remaining <= 0,
        "started_at": started.isoformat(),
    }

# ─────────────────────────────────────────────────────────────
# health check  (يُستخدم لإيقاظ السيرفر قبل عملية ثقيلة)
# ─────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok", "service": "smartpdf", "ts": int(time.time())}

# ─────────────────────────────────────────────────────────────
# سياسة الخصوصية
# ─────────────────────────────────────────────────────────────
@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return """<!doctype html><html lang="ar" dir="rtl"><head>
<meta charset="utf-8"><title>سياسة الخصوصية</title></head>
<body style="font-family:sans-serif;max-width:720px;margin:40px auto;padding:0 16px;line-height:1.8">
<h1>سياسة الخصوصية — Smart PDF</h1>
<p>الأدوات التي تعمل دون اتصال (مثل العلامة المائية) تعالج ملفاتك على
جهازك ولا تغادر الملفات الجهاز.</p>
<p>الأدوات التي تحتاج اتصالاً (مثل الضغط) ترفع الملف مؤقتاً للمعالجة
ثم يُحذف فوراً، ولا يُخزَّن أو يُشارَك.</p>
</body></html>"""

# ─────────────────────────────────────────────────────────────
# جلسات المعاينة (في الذاكرة)  sid -> {doc, pages, expires}
# ─────────────────────────────────────────────────────────────
_sessions = {}
_sess_lock = threading.Lock()

def _gc_sessions():
    now = time.time()
    dead = [k for k, v in _sessions.items() if v["expires"] < now]
    for k in dead:
        try:
            _sessions[k]["doc"].close()
        except Exception:
            pass
        _sessions.pop(k, None)

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """يرفع PDF ويفتح جلسة معاينة، يُرجع sessionId و totalPages."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413,
                            detail=f"File too large (>{MAX_UPLOAD_MB}MB)")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid PDF")

    with _sess_lock:
        _gc_sessions()
        if len(_sessions) >= MAX_SESSIONS:
            # أزل الأقدم
            oldest = min(_sessions, key=lambda k: _sessions[k]["expires"])
            try:
                _sessions[oldest]["doc"].close()
            except Exception:
                pass
            _sessions.pop(oldest, None)
        sid = f"s{int(time.time()*1000)}{len(_sessions)}"
        _sessions[sid] = {
            "doc": doc,
            "pages": doc.page_count,
            "expires": time.time() + SESSION_TTL,
        }
    return {"sessionId": sid, "totalPages": doc.page_count}

@app.get("/render/{sid}/{page}")
async def render_page(sid: str = Path(...), page: int = Path(...)):
    """يُرجع صورة PNG لصفحة (1-based)."""
    with _sess_lock:
        _gc_sessions()
        sess = _sessions.get(sid)
        if sess is None:
            raise HTTPException(status_code=410, detail="Session expired")
        sess["expires"] = time.time() + SESSION_TTL
        doc = sess["doc"]
        total = sess["pages"]

    if page < 1 or page > total:
        raise HTTPException(status_code=404, detail="Page out of range")

    p = doc.load_page(page - 1)
    pix = p.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
    return Response(content=pix.tobytes("png"), media_type="image/png")

@app.post("/render-page")
async def render_page_legacy(file: UploadFile = File(...), page: int = Form(0)):
    """(legacy) معاينة صفحة واحدة من ملف مرفوع مباشرة."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid PDF")
    if page < 0 or page >= doc.page_count:
        doc.close()
        raise HTTPException(status_code=404, detail="Page out of range")
    pix = doc.load_page(page).get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
    out = pix.tobytes("png")
    doc.close()
    return Response(content=out, media_type="image/png")

# ─────────────────────────────────────────────────────────────
# الضغط  (مُصلح: المستويات تعمل فعلاً + يعالج الملفات الكبيرة)
# ─────────────────────────────────────────────────────────────
#
# الإصلاح الجوهري: rewrite_images يتطلب dpi_target < dpi_threshold.
# الكود القديم مرّر نفس القيمة للحقلين فرمى استثناءً وسقط للوضع
# الافتراضي — فكانت كل المستويات متطابقة فعلياً. الآن لكل مستوى
# قيم صحيحة متمايزة.
#
COMPRESS_PRESETS = {
    "low":    dict(dpi_threshold=200, dpi_target=150, quality=70),  # خفيف
    "medium": dict(dpi_threshold=150, dpi_target=110, quality=55),  # متوازن
    "high":   dict(dpi_threshold=120, dpi_target=90,  quality=40),  # أقصى
}

@app.post("/compress")
async def compress_pdf(
    file: UploadFile = File(...),
    level: str = Form("medium"),
):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_COMPRESS_MB * 1024 * 1024:
        raise HTTPException(status_code=413,
                            detail=f"File too large (>{MAX_COMPRESS_MB}MB)")

    cfg = COMPRESS_PRESETS.get(level, COMPRESS_PRESETS["medium"])
    original_kb = round(len(data) / 1024)

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid PDF")

    try:
        # أعد ضغط الصور (المصدر الأكبر للحجم) — الدالة الرسمية الآمنة
        doc.rewrite_images(
            dpi_threshold=cfg["dpi_threshold"],
            dpi_target=cfg["dpi_target"],
            quality=cfg["quality"],
            lossy=True,
            lossless=True,
        )
        # نظّف وادمج (garbage) واضغط البنية (deflate)
        out = doc.tobytes(garbage=4, deflate=True, clean=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Compress failed: {e}")
    finally:
        doc.close()

    if not out or out[:5] != b"%PDF-":
        raise HTTPException(status_code=500, detail="Invalid output")

    compressed_kb = round(len(out) / 1024)
    # لو لم ينفع الضغط (الملف أصلاً مضغوط)، أعد الأصل لتفادي تكبيره
    if compressed_kb >= original_kb:
        out = data
        compressed_kb = original_kb

    headers = {
        "x-original-kb":   str(original_kb),
        "x-compressed-kb": str(compressed_kb),
        "x-saved-percent": str(max(0, round((1 - compressed_kb / max(1, original_kb)) * 100))),
        "Content-Disposition": 'attachment; filename="compressed.pdf"',
    }
    return Response(content=out, media_type="application/pdf", headers=headers)
