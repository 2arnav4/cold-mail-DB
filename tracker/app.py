import os
import io
import base64
import sqlite3
from datetime import datetime
from flask import Flask, request, send_file, jsonify, render_template_string

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", "/tmp/tracker.db")

# ── 1×1 transparent GIF ──────────────────────────────────────────────────────
PIXEL = (
    b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00"
    b"\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x00\x00\x00\x00"
    b"\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02"
    b"\x44\x01\x00\x3b"
)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS opens (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                email       TEXT    NOT NULL,
                company     TEXT    DEFAULT '',
                opened_at   TEXT    NOT NULL,
                ip          TEXT    DEFAULT '',
                user_agent  TEXT    DEFAULT '',
                UNIQUE(email, opened_at)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sends (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT    NOT NULL UNIQUE,
                company       TEXT    DEFAULT '',
                sent_at       TEXT    NOT NULL,
                status        TEXT    DEFAULT 'sent',
                bounce_reason TEXT    DEFAULT '',
                bounce_type   TEXT    DEFAULT '',
                retry_after   TEXT    DEFAULT ''
            )
        """)
        for col, definition in [
            ("status",        "TEXT DEFAULT 'sent'"),
            ("bounce_reason", "TEXT DEFAULT ''"),
            ("bounce_type",   "TEXT DEFAULT ''"),
            ("retry_after",   "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE sends ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass
        conn.commit()

# ── Keep-alive ping ───────────────────────────────────────────────────────────
@app.route("/ping")
def ping():
    return jsonify({"status": "ok", "ts": datetime.utcnow().isoformat()}), 200

# ── Log a sent email ──────────────────────────────────────────────────────────
@app.route("/api/log_send", methods=["POST"])
def log_send():
    data    = request.get_json() or {}
    email   = data.get("email")
    company = data.get("company", "")
    sent_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if not email:
        return jsonify({"error": "Missing email"}), 400
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO sends (email, company, sent_at, status, bounce_reason, bounce_type, retry_after) "
                "VALUES (?, ?, ?, 'sent', '', '', '') "
                "ON CONFLICT(email) DO UPDATE SET company=excluded.company, sent_at=excluded.sent_at, "
                "status='sent', bounce_reason='', bounce_type='', retry_after=''",
                (email, company, sent_at)
            )
            conn.commit()
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Log a bounced email ───────────────────────────────────────────────────────
@app.route("/api/log_bounce", methods=["POST"])
def log_bounce():
    data        = request.get_json() or {}
    email       = data.get("email")
    reason      = data.get("reason",      "Bounce — unknown reason")
    bounce_type = data.get("bounce_type", "hard")
    retry_after = data.get("retry_after", "")
    if not email:
        return jsonify({"error": "Missing email"}), 400
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE sends SET status='bounced', bounce_reason=?, bounce_type=?, retry_after=? WHERE email=?",
                (reason, bounce_type, retry_after, email)
            )
            conn.execute(
                "INSERT OR IGNORE INTO sends (email, status, bounce_reason, bounce_type, retry_after, sent_at) "
                "VALUES (?, 'bounced', ?, ?, ?, ?)",
                (email, reason, bounce_type, retry_after, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn.commit()
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Bulk sync ─────────────────────────────────────────────────────────────────
@app.route("/api/bulk_sync", methods=["POST"])
def bulk_sync():
    data    = request.get_json() or {}
    sends   = data.get("sends",   [])
    bounces = data.get("bounces", [])
    opens   = data.get("opens",   [])
    try:
        with get_db() as conn:
            for s in sends:
                conn.execute(
                    "INSERT OR IGNORE INTO sends (email, company, sent_at, status, bounce_reason, bounce_type, retry_after) "
                    "VALUES (?, ?, ?, 'sent', '', '', '')",
                    (s.get("email",""), s.get("company",""), s.get("sent_at",""))
                )
            for b in bounces:
                conn.execute(
                    "INSERT INTO sends (email, company, sent_at, status, bounce_reason, bounce_type, retry_after) "
                    "VALUES (?, ?, ?, 'bounced', ?, ?, ?) "
                    "ON CONFLICT(email) DO UPDATE SET status='bounced', bounce_reason=excluded.bounce_reason, "
                    "bounce_type=excluded.bounce_type, retry_after=excluded.retry_after",
                    (b.get("email",""), b.get("company",""), b.get("sent_at",""),
                     b.get("reason","Bounce — invalid address"),
                     b.get("bounce_type","hard"),
                     b.get("retry_after",""))
                )
            for o in opens:
                conn.execute(
                    "INSERT OR IGNORE INTO opens (email, company, opened_at, ip, user_agent) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (o.get("email",""), o.get("company",""), o.get("opened_at",""), o.get("ip",""), o.get("user_agent",""))
                )
            conn.commit()
        return jsonify({"synced_sends": len(sends), "synced_bounces": len(bounces), "synced_opens": len(opens)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Tracking pixel ────────────────────────────────────────────────────────────
@app.route("/t/<path:encoded>.gif")
def track(encoded):
    try:
        padding = 4 - len(encoded) % 4
        decoded = base64.urlsafe_b64decode(encoded + "=" * padding).decode()
        parts   = decoded.split("|", 1)
        email   = parts[0]
        company = parts[1] if len(parts) > 1 else ""
    except Exception:
        email, company = "unknown", ""

    ip        = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    ua        = request.headers.get("User-Agent", "")
    opened_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sends (email, company, sent_at) VALUES (?, ?, ?)",
                (email, company, opened_at)
            )
            conn.execute(
                "INSERT INTO opens (email, company, opened_at, ip, user_agent) VALUES (?,?,?,?,?)",
                (email, company, opened_at, ip, ua),
            )
            conn.commit()
    except Exception as e:
        print(f"DB error: {e}")

    return send_file(io.BytesIO(PIXEL), mimetype="image/gif", max_age=0, etag=False)

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cold Mail Tracker</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  :root{--bg:#0d0f18;--surface:#14172b;--surface2:#1c2038;--border:#252a45;--accent:#6c63ff;--accent2:#00d4aa;--text:#e2e8f0;--muted:#64748b;--green:#22c55e;--blue:#3b82f6;--red:#ef4444;--orange:#f97316;--yellow:#eab308}
  body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:2rem}
  .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:2rem}
  h1{font-size:1.75rem;font-weight:700;letter-spacing:-.5px;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .subtitle{color:var(--muted);font-size:.875rem;margin-top:.25rem}
  .badge{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;padding:.3rem .9rem;border-radius:9999px;font-size:.75rem;font-weight:600;box-shadow:0 0 16px rgba(108,99,255,.4)}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;margin-bottom:2rem}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:1.25rem;transition:border-color .2s,transform .2s}
  .card:hover{border-color:var(--accent);transform:translateY(-2px)}
  .card-label{font-size:.7rem;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.07em}
  .card-value{font-size:2.25rem;font-weight:700;margin-top:.4rem}
  .grad{background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .red-g{background:linear-gradient(135deg,#ef4444,#f97316);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .blue-g{background:linear-gradient(135deg,#3b82f6,#06b6d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .green-g{background:linear-gradient(135deg,#22c55e,#16a34a);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .yellow-g{background:linear-gradient(135deg,#eab308,#f97316);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .toolbar{display:flex;gap:.75rem;margin-bottom:1.5rem;align-items:center;flex-wrap:wrap}
  .search-input{flex:1;min-width:240px;max-width:400px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:.65rem 1rem;color:var(--text);font-family:inherit;font-size:.875rem}
  .search-input:focus{outline:none;border-color:var(--accent)}
  .filter-btn{background:var(--surface2);border:1px solid var(--border);color:var(--muted);padding:.6rem 1rem;border-radius:8px;font-size:.8rem;cursor:pointer;transition:all .2s;font-family:inherit}
  .filter-btn:hover,.filter-btn.active{border-color:var(--accent);color:var(--text)}
  .filter-btn.f-opened.active{border-color:var(--blue);color:var(--blue)}
  .filter-btn.f-bounced.active{border-color:var(--red);color:var(--red)}
  .filter-btn.f-sent.active{border-color:var(--green);color:var(--green)}
  .section{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:1.5rem;margin-bottom:1.5rem;overflow-x:auto}
  .section-title{font-size:1rem;font-weight:600;margin-bottom:1rem}
  table{width:100%;border-collapse:collapse;font-size:.825rem;min-width:860px}
  th{text-align:left;padding:.6rem .75rem;color:var(--muted);font-weight:600;font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:.7rem .75rem;border-bottom:1px solid rgba(37,42,69,.6);vertical-align:middle}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(108,99,255,.04)}
  .email-cell{font-family:monospace;font-size:.8rem;color:#a5b4fc}
  .company-cell{color:var(--muted)}
  .bounce-reason{font-size:.75rem;color:var(--orange);max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .pill{display:inline-flex;align-items:center;gap:.3rem;padding:.2rem .65rem;border-radius:9999px;font-size:.7rem;font-weight:600;white-space:nowrap}
  .pill-opened{background:rgba(59,130,246,.15);color:var(--blue)}
  .pill-sent{background:rgba(34,197,94,.15);color:var(--green)}
  .pill-bounced{background:rgba(239,68,68,.15);color:var(--red)}
  .pill-hard{background:rgba(239,68,68,.1);color:#fca5a5;font-size:.65rem;border:1px solid rgba(239,68,68,.3)}
  .pill-soft{background:rgba(234,179,8,.1);color:var(--yellow);font-size:.65rem;border:1px solid rgba(234,179,8,.3)}
  .pill-retry{background:rgba(249,115,22,.1);color:var(--orange);font-size:.65rem}
  .empty{text-align:center;color:var(--muted);padding:3rem;font-size:.875rem}
</style>
<script>
var currentFilter='all';
function filterTable(){
  var q=document.getElementById('search').value.toLowerCase();
  document.querySelectorAll('#email-table tbody tr').forEach(function(r){
    var match=(r.dataset.email||'').toLowerCase().includes(q)||(r.dataset.company||'').toLowerCase().includes(q);
    var fmatch=currentFilter==='all'||(r.dataset.status===currentFilter);
    r.style.display=(match&&fmatch)?'':'none';
  });
}
function setFilter(f){
  currentFilter=f;
  document.querySelectorAll('.filter-btn').forEach(function(b){b.classList.toggle('active',b.dataset.filter===f);});
  filterTable();
}
document.addEventListener("DOMContentLoaded", function() {
  const months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  document.querySelectorAll(".datetime-cell").forEach(function(cell) {
    const utcStr = cell.getAttribute("data-utc");
    if (!utcStr) {
      cell.textContent = "—";
      return;
    }
    // Replace space with T and append Z to force parsing as UTC
    const date = new Date(utcStr.replace(' ', 'T') + 'Z');
    if (isNaN(date.getTime())) return;

    const day = date.getDate();
    const month = months[date.getMonth()];
    const year = date.getFullYear();

    let suffix = "th";
    if (day < 11 || day > 13) {
      switch (day % 10) {
        case 1: suffix = "st"; break;
        case 2: suffix = "nd"; break;
        case 3: suffix = "rd"; break;
      }
    }

    let hours = date.getHours();
    const minutes = String(date.getMinutes()).padStart(2, '0');
    const ampm = hours >= 12 ? 'PM' : 'AM';
    hours = hours % 12;
    hours = hours ? hours : 12;

    cell.textContent = day + suffix + " " + month + " " + year + ", " + hours + ":" + minutes + ampm;
  });
});
</script>
</head>
<body>
<div class="header">
  <div><h1>📬 Cold Mail Tracker</h1><div class="subtitle">Real-time outreach — opens, bounce type &amp; retry status</div></div>
  <div class="badge">● Live</div>
</div>
<div class="cards">
  <div class="card"><div class="card-label">Total Sent</div><div class="card-value grad">{{ total_sent }}</div></div>
  <div class="card"><div class="card-label">Unique Opens</div><div class="card-value blue-g">{{ unique_opens }}</div></div>
  <div class="card"><div class="card-label">Total Opens</div><div class="card-value grad">{{ total_opens }}</div></div>
  <div class="card"><div class="card-label">Hard Bounces</div><div class="card-value red-g">{{ hard_bounces }}</div></div>
  <div class="card"><div class="card-label">Soft Bounces</div><div class="card-value yellow-g">{{ soft_bounces }}</div></div>
  <div class="card"><div class="card-label">Bounce Rate</div><div class="card-value red-g">{{ bounce_rate }}%</div></div>
  <div class="card"><div class="card-label">Open Rate</div><div class="card-value green-g">{{ open_rate }}%</div></div>
</div>
<div class="toolbar">
  <input type="text" id="search" oninput="filterTable()" class="search-input" placeholder="Search email or company…">
  <button class="filter-btn active" data-filter="all"     onclick="setFilter('all')">All</button>
  <button class="filter-btn f-sent"    data-filter="sent"    onclick="setFilter('sent')">✉ Sent</button>
  <button class="filter-btn f-opened"  data-filter="opened"  onclick="setFilter('opened')">👁 Opened</button>
  <button class="filter-btn f-bounced" data-filter="bounced" onclick="setFilter('bounced')">⚠ Bounced</button>
</div>
<div class="section">
  <div class="section-title">📧 Outreach Status</div>
  {% if outreach %}
  <table id="email-table">
    <thead><tr>
      <th>Email</th><th>Company</th><th>Sent</th><th>Last Open</th>
      <th>Opens</th><th>Status</th><th>Bounce Reason</th><th>Type / Retry</th>
    </tr></thead>
    <tbody>
    {% for row in outreach %}
    <tr data-email="{{ row.email }}" data-company="{{ row.company }}"
        data-status="{{ 'bounced' if row.status == 'bounced' else ('opened' if row.opens_count and row.opens_count > 0 else 'sent') }}">
      <td class="email-cell">{{ row.email }}</td>
      <td class="company-cell">{{ row.company or '—' }}</td>
      <td class="datetime-cell" data-utc="{{ row.sent_at or '' }}" style="color:var(--muted);font-size:.78rem;">{{ row.sent_at or '—' }}</td>
      <td class="datetime-cell" data-utc="{{ row.opened_at or '' }}" style="color:var(--muted);font-size:.78rem;">{{ row.opened_at or '—' }}</td>
      <td style="text-align:center;">
        {% if row.opens_count and row.opens_count > 0 %}
          <span style="color:var(--blue);font-weight:700;">{{ row.opens_count }}</span>
        {% else %}—{% endif %}
      </td>
      <td>
        {% if row.status == 'bounced' %}<span class="pill pill-bounced">⚠ Bounced</span>
        {% elif row.opens_count and row.opens_count > 0 %}<span class="pill pill-opened">👁 Opened</span>
        {% else %}<span class="pill pill-sent">✉ Sent</span>{% endif %}
      </td>
      <td class="bounce-reason" title="{{ row.bounce_reason or '' }}">
        {% if row.bounce_reason %}{{ row.bounce_reason }}{% else %}—{% endif %}
      </td>
      <td>
        {% if row.status == 'bounced' %}
          {% if row.bounce_type == 'soft' %}
            <span class="pill pill-soft">Soft</span>
            {% if row.retry_after %}&nbsp;<span class="pill pill-retry">↺ {{ row.retry_after }}</span>{% endif %}
          {% else %}
            <span class="pill pill-hard">Hard · Denied</span>
          {% endif %}
        {% else %}—{% endif %}
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">No emails tracked yet. Start your outreach campaign!</div>
  {% endif %}
</div>
</body>
</html>"""


@app.route("/stats")
def stats():
    with get_db() as conn:
        total_sent   = conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0]
        total_opens  = conn.execute("SELECT COUNT(*) FROM opens").fetchone()[0]
        unique_opens = conn.execute("SELECT COUNT(DISTINCT email) FROM opens").fetchone()[0]
        hard_bounces = conn.execute("SELECT COUNT(*) FROM sends WHERE status='bounced' AND bounce_type!='soft'").fetchone()[0]
        soft_bounces = conn.execute("SELECT COUNT(*) FROM sends WHERE status='bounced' AND bounce_type='soft'").fetchone()[0]
        outreach     = conn.execute("""
            SELECT s.email, s.company, s.sent_at, s.status,
                   s.bounce_reason, s.bounce_type, s.retry_after,
                   (SELECT COUNT(*) FROM opens o WHERE o.email=s.email) as opens_count,
                   (SELECT MAX(opened_at) FROM opens o WHERE o.email=s.email) as opened_at
            FROM sends s ORDER BY s.sent_at DESC
        """).fetchall()
    open_rate   = round((unique_opens / max(total_sent,1)) * 100) if total_sent else 0
    bounce_rate = round(((hard_bounces + soft_bounces) / max(total_sent,1)) * 100) if total_sent else 0
    return render_template_string(DASHBOARD,
        total_sent=total_sent, total_opens=total_opens, unique_opens=unique_opens,
        hard_bounces=hard_bounces, soft_bounces=soft_bounces, bounce_rate=bounce_rate,
        open_rate=open_rate, outreach=outreach)


@app.route("/api/stats")
def api_stats():
    with get_db() as conn:
        rows = conn.execute("SELECT email, company, opened_at, ip, user_agent FROM opens ORDER BY opened_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/")
def index():
    return "Cold Mail Tracker ✅ — visit /stats for dashboard"


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
