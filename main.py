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
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Path, Request
from fastapi.responses import Response, JSONResponse, HTMLResponse, PlainTextResponse

app = FastAPI(title="Smart PDF Server")

# ─────────────────────────────────────────────────────────────
# الإعدادات
# ─────────────────────────────────────────────────────────────
TRIAL_DAYS      = 7           # المدة الافتراضية للأجهزة الجديدة
ADMIN_TOKEN     = "CHANGE_ME_smartpdf_2026"  # ⚠️ غيّره لكلمة سر قوية — يحمي نقاط الإدارة
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
                started_at TEXT NOT NULL,
                trial_days INTEGER NOT NULL DEFAULT 7
            )
        """)
        # ترحيل: أضف عمود trial_days للجداول القديمة إن لم يكن موجوداً
        cols = [r[1] for r in c.execute("PRAGMA table_info(trials)").fetchall()]
        if "trial_days" not in cols:
            c.execute(f"ALTER TABLE trials ADD COLUMN trial_days INTEGER NOT NULL DEFAULT {TRIAL_DAYS}")
        c.commit()

_init_db()

def _trial_status(device_id: str):
    """يقرأ (أو يُنشئ) سجل الجهاز ويعيد الحالة الكاملة. الخادم مصدر الحقيقة."""
    now = datetime.now(timezone.utc)
    with _db_lock, sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT started_at, trial_days FROM trials WHERE device_id=?", (device_id,)
        ).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO trials(device_id, started_at, trial_days) VALUES(?,?,?)",
                (device_id, now.isoformat(), TRIAL_DAYS),
            )
            c.commit()
            started, days = now, TRIAL_DAYS
        else:
            started = datetime.fromisoformat(row[0])
            days = int(row[1])

    elapsed_days = (now - started).total_seconds() / 86400.0
    remaining = max(0, days - int(elapsed_days))
    return {
        "device_id": device_id,
        "trial_days": days,
        "started_at": started.isoformat(),
        # نرسل أسماء متعددة للتوافق مع التطبيق (firstSeen + daysLeft + snake_case)
        "firstSeen": started.isoformat(),
        "daysLeft": remaining,
        "days_remaining": remaining,
        "expired": remaining <= 0,
    }


async def _read_device_id(request: Request) -> str:
    """يقبل device_id سواء أُرسل كـ JSON (deviceId/device_id) أو Form."""
    dev = None
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            body = await request.json()
            dev = body.get("deviceId") or body.get("device_id")
        except Exception:
            dev = None
    else:
        try:
            form = await request.form()
            dev = form.get("device_id") or form.get("deviceId")
        except Exception:
            dev = None
    if not dev or len(str(dev)) < 4:
        raise HTTPException(status_code=400, detail="Invalid device id")
    return str(dev)


@app.post("/trial/check")
async def trial_check(request: Request):
    """تحقق/تسجيل تجربة الجهاز. الخادم مصدر الحقيقة للمدة والأيام المتبقية."""
    device_id = await _read_device_id(request)
    return _trial_status(device_id)


@app.post("/trial/extend")
async def trial_extend(request: Request):
    """
    تمديد تجربة جهاز معيّن (إداري — يتطلب التوكن).
    الاستخدام: POST مع JSON { "token": "...", "device_id": "...", "days": 20 }
      • days = المدة الإجمالية الجديدة (لا إضافة). مثال: 20 = تصبح المدة 20 يوماً.
      • reset_start=true (اختياري) => يعيد بداية التجربة من الآن (تمديد فعلي كامل).
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required")

    if body.get("token") != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    device_id = body.get("device_id") or body.get("deviceId")
    if not device_id or len(str(device_id)) < 4:
        raise HTTPException(status_code=400, detail="Invalid device id")

    days = int(body.get("days", TRIAL_DAYS))
    reset_start = bool(body.get("reset_start", False))
    now = datetime.now(timezone.utc)

    with _db_lock, sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT started_at FROM trials WHERE device_id=?", (device_id,)).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO trials(device_id, started_at, trial_days) VALUES(?,?,?)",
                (device_id, now.isoformat(), days),
            )
        else:
            if reset_start:
                c.execute(
                    "UPDATE trials SET trial_days=?, started_at=? WHERE device_id=?",
                    (days, now.isoformat(), device_id),
                )
            else:
                c.execute(
                    "UPDATE trials SET trial_days=? WHERE device_id=?",
                    (days, device_id),
                )
        c.commit()

    return _trial_status(str(device_id))


@app.get("/trial/list")
async def trial_list(token: str = ""):
    """عرض كل الأجهزة وحالتها (إداري). الاستخدام: /trial/list?token=..."""
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    now = datetime.now(timezone.utc)
    out = []
    with _db_lock, sqlite3.connect(DB_PATH) as c:
        for did, started_at, days in c.execute(
            "SELECT device_id, started_at, trial_days FROM trials"
        ).fetchall():
            started = datetime.fromisoformat(started_at)
            elapsed = (now - started).total_seconds() / 86400.0
            remaining = max(0, int(days) - int(elapsed))
            out.append({
                "device_id": did, "started_at": started_at,
                "trial_days": days, "days_remaining": remaining,
                "expired": remaining <= 0,
            })
    return {"count": len(out), "devices": out}

# ─────────────────────────────────────────────────────────────
# health check  (يُستخدم لإيقاظ السيرفر قبل عملية ثقيلة)
# ─────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok", "service": "smartpdf", "ts": int(time.time())}

# ─────────────────────────────────────────────────────────────
# لوحة التحكم الإدارية  (/admin)
# صفحة ويب واحدة: عرض كل الأجهزة + تعديل المدة لأي جهاز.
# محميّة بالتوكن (يُدخله المستخدم في الصفحة، ويُرسل مع كل طلب).
# ─────────────────────────────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return """<!doctype html>
<html lang="ar" dir="rtl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smart PDF — لوحة التحكم</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, 'Segoe UI', Tahoma, sans-serif; background:#0f172a; color:#f1f5f9; margin:0; padding:0; }
  .wrap { max-width: 900px; margin: 0 auto; padding: 20px 16px 60px; }
  h1 { font-size: 20px; font-weight: 700; margin: 8px 0 4px; }
  .sub { color:#94a3b8; font-size:13px; margin-bottom:20px; }
  .login { background:#1e293b; border:1px solid #334155; border-radius:14px; padding:20px; max-width:420px; margin:40px auto; }
  .login label { display:block; font-size:13px; color:#94a3b8; margin-bottom:8px; }
  input { width:100%; background:#0f172a; border:1px solid #334155; color:#f1f5f9; border-radius:10px; padding:11px 12px; font-size:14px; }
  input:focus { outline:none; border-color:#7C3AED; }
  button { background:#7C3AED; color:#fff; border:none; border-radius:10px; padding:11px 16px; font-size:14px; font-weight:600; cursor:pointer; }
  button:active { opacity:.85; }
  button.sm { padding:7px 12px; font-size:13px; }
  button.gray { background:#334155; }
  .bar { display:flex; gap:10px; align-items:center; margin-bottom:16px; flex-wrap:wrap; }
  .bar .stat { background:#1e293b; border:1px solid #334155; border-radius:10px; padding:8px 14px; font-size:13px; }
  .card { background:#1e293b; border:1px solid #334155; border-radius:14px; padding:14px; margin-bottom:12px; }
  .did { font-family:monospace; font-size:13px; color:#c4b5fd; word-break:break-all; }
  .meta { display:flex; gap:16px; flex-wrap:wrap; margin:10px 0; font-size:13px; color:#94a3b8; }
  .meta b { color:#f1f5f9; font-weight:600; }
  .badge { display:inline-block; padding:2px 10px; border-radius:20px; font-size:12px; font-weight:600; }
  .ok { background:#14532d; color:#4ade80; }
  .no { background:#4c1d1d; color:#f87171; }
  .actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:10px; }
  .actions input { width:70px; }
  .hint { font-size:12px; color:#64748b; }
  .msg { margin-top:6px; font-size:13px; min-height:18px; }
  .hidden { display:none; }
  .spin { color:#94a3b8; text-align:center; padding:30px; }
</style></head>
<body>
<div class="wrap">

  <!-- شاشة الدخول -->
  <div id="loginBox" class="login">
    <h1>🔒 لوحة تحكم Smart PDF</h1>
    <label>أدخل رمز الإدارة (Admin Token)</label>
    <input id="tokenInput" type="password" placeholder="التوكن السري" />
    <div style="height:12px"></div>
    <button onclick="login()" style="width:100%">دخول</button>
    <div id="loginMsg" class="msg"></div>
  </div>

  <!-- اللوحة -->
  <div id="panel" class="hidden">
    <h1>لوحة تحكم Smart PDF</h1>
    <div class="sub">إدارة تجارب المستخدمين — عرض وتحديث المدة لأي جهاز.</div>
    <div class="bar">
      <button class="sm" onclick="load()">🔄 تحديث القائمة</button>
      <div class="stat">الأجهزة: <b id="count">0</b></div>
      <button class="sm gray" onclick="logout()">خروج</button>
    </div>
    <div id="list"><div class="spin">جارٍ التحميل…</div></div>
  </div>

</div>

<script>
  var TOKEN = '';
  var BASE = window.location.origin;

  function login() {
    var t = document.getElementById('tokenInput').value.trim();
    if (!t) { msg('loginMsg', 'أدخل التوكن'); return; }
    TOKEN = t;
    // نتحقق بجلب القائمة
    fetch(BASE + '/trial/list?token=' + encodeURIComponent(TOKEN))
      .then(function(r){ if(!r.ok) throw new Error('توكن غير صحيح'); return r.json(); })
      .then(function(){
        document.getElementById('loginBox').classList.add('hidden');
        document.getElementById('panel').classList.remove('hidden');
        try { sessionStorage.setItem('spdf_admin', TOKEN); } catch(e){}
        load();
      })
      .catch(function(e){ msg('loginMsg', '❌ ' + e.message); });
  }

  function logout() {
    TOKEN = '';
    try { sessionStorage.removeItem('spdf_admin'); } catch(e){}
    document.getElementById('panel').classList.add('hidden');
    document.getElementById('loginBox').classList.remove('hidden');
  }

  function load() {
    document.getElementById('list').innerHTML = '<div class="spin">جارٍ التحميل…</div>';
    fetch(BASE + '/trial/list?token=' + encodeURIComponent(TOKEN))
      .then(function(r){ return r.json(); })
      .then(function(data){
        var devs = data.devices || [];
        document.getElementById('count').textContent = devs.length;
        if (devs.length === 0) {
          document.getElementById('list').innerHTML = '<div class="spin">لا توجد أجهزة مسجّلة بعد.</div>';
          return;
        }
        var html = '';
        window._devs = devs;
        for (var i=0; i<devs.length; i++) {
          var d = devs[i];
          var active = !d.expired;
          html += '<div class="card">'
            + '<div class="did">' + esc(d.device_id) + '</div>'
            + '<div class="meta">'
            +   '<span>الحالة: <span class="badge ' + (active?'ok':'no') + '">' + (active?'فعّالة':'منتهية') + '</span></span>'
            +   '<span>المتبقّي: <b>' + d.days_remaining + '</b> يوم</span>'
            +   '<span>المدة: <b>' + d.trial_days + '</b> يوم</span>'
            +   '<span>البدء: <b>' + fmt(d.started_at) + '</b></span>'
            + '</div>'
            + '<div class="actions">'
            +   '<span class="hint">مدة جديدة:</span>'
            +   '<input id="days_' + i + '" type="number" min="1" value="20" />'
            +   '<button class="sm" onclick="extend(' + i + ', true)">تمديد كامل (يبدأ الآن)</button>'
            +   '<button class="sm gray" onclick="extend(' + i + ', false)">تعديل المدة فقط</button>'
            + '</div>'
            + '<div class="msg" id="msg_' + i + '"></div>'
            + '</div>';
        }
        document.getElementById('list').innerHTML = html;
      })
      .catch(function(e){
        document.getElementById('list').innerHTML = '<div class="spin">خطأ: ' + esc(e.message) + '</div>';
      });
  }

  function extend(idx, resetStart) {
    var deviceId = window._devs[idx].device_id;
    var days = parseInt(document.getElementById('days_' + idx).value, 10);
    if (!days || days < 1) { msg('msg_'+idx, 'أدخل عدد أيام صحيح'); return; }
    msg('msg_'+idx, '⏳ جارٍ التحديث…');
    fetch(BASE + '/trial/extend', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: TOKEN, device_id: deviceId, days: days, reset_start: resetStart })
    })
    .then(function(r){ if(!r.ok) throw new Error('فشل (' + r.status + ')'); return r.json(); })
    .then(function(res){
      msg('msg_'+idx, '✅ تم — المتبقّي الآن ' + res.days_remaining + ' يوم');
      setTimeout(load, 900);
    })
    .catch(function(e){ msg('msg_'+idx, '❌ ' + e.message); });
  }

  function msg(id, t){ var el=document.getElementById(id); if(el) el.textContent=t; }
  function esc(s){ var d=document.createElement('div'); d.textContent=s==null?'':String(s); return d.innerHTML; }
  function fmt(iso){ try { return new Date(iso).toLocaleDateString('ar-EG'); } catch(e){ return iso; } }

  // استعادة الجلسة إن وُجدت
  (function(){
    try {
      var saved = sessionStorage.getItem('spdf_admin');
      if (saved) { document.getElementById('tokenInput').value = saved; login(); }
    } catch(e){}
  })();
</script>
</body></html>"""


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
    "low":    dict(dpi_threshold=150, dpi_target=110, quality=65),  # خفيف
    "medium": dict(dpi_threshold=110, dpi_target=84,  quality=55),  # متوازن
    "high":   dict(dpi_threshold=90,  dpi_target=60,  quality=42),  # أقصى توفير
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
        # أعد ضغط الصور (المصدر الأكبر للحجم) — الدالة الرسمية الآمنة.
        # lossy فقط (بلا lossless) لتجنّب مضاعفة زمن المعالجة على Render المجاني.
        doc.rewrite_images(
            dpi_threshold=cfg["dpi_threshold"],
            dpi_target=cfg["dpi_target"],
            quality=cfg["quality"],
            lossy=True,
            lossless=False,
        )
        # ادمج البنية واضغطها (garbage=3 أسرع من 4 على المستندات الكبيرة)
        out = doc.tobytes(garbage=3, deflate=True)
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
