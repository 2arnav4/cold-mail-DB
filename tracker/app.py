import os
import io
import base64
import sqlite3
from datetime import datetime
from flask import Flask, request, send_file, jsonify, render_template_string

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", "tracker.db")

# ── 1×1 transparent GIF ──────────────────────────────────────────────────────
PIXEL = (
    b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00"
    b"\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x00\x00\x00\x00"
    b"\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02"
    b"\x44\x01\x00\x3b"
)

# ── Database ──────────────────────────────────────────────────────────────────
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
                user_agent  TEXT    DEFAULT ''
            )
        """)
        conn.commit()


# ── Tracking pixel endpoint ───────────────────────────────────────────────────
@app.route("/t/<path:encoded>.gif")
def track(encoded):
    # Decode base64url(email|company)
    try:
        padding  = 4 - len(encoded) % 4
        decoded  = base64.urlsafe_b64decode(encoded + "=" * padding).decode()
        parts    = decoded.split("|", 1)
        email    = parts[0]
        company  = parts[1] if len(parts) > 1 else ""
    except Exception:
        email, company = "unknown", ""

    ip         = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    ua         = request.headers.get("User-Agent", "")
    opened_at  = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO opens (email, company, opened_at, ip, user_agent) VALUES (?,?,?,?,?)",
                (email, company, opened_at, ip, ua),
            )
            conn.commit()
    except Exception as e:
        print(f"DB error: {e}")

    return send_file(io.BytesIO(PIXEL), mimetype="image/gif",
                     max_age=0, etag=False)


# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cold Mail Tracker</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:      #0f1117;
    --surface: #1a1d27;
    --border:  #2a2d3e;
    --accent:  #6c63ff;
    --accent2: #00d4aa;
    --text:    #e2e8f0;
    --muted:   #64748b;
    --green:   #22c55e;
    --red:     #ef4444;
  }

  body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text);
         min-height: 100vh; padding: 2rem; }

  h1   { font-size: 1.75rem; font-weight: 700; letter-spacing: -0.5px;
         background: linear-gradient(135deg, var(--accent), var(--accent2));
         -webkit-background-clip: text; -webkit-text-fill-color: transparent; }

  .subtitle { color: var(--muted); font-size: 0.875rem; margin-top: 0.25rem; }

  .header   { display: flex; align-items: center; justify-content: space-between;
              margin-bottom: 2rem; }

  .badge    { background: var(--accent); color: #fff; padding: 0.25rem 0.75rem;
              border-radius: 9999px; font-size: 0.75rem; font-weight: 600; }

  /* Stat cards */
  .cards    { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
              gap: 1rem; margin-bottom: 2rem; }

  .card     { background: var(--surface); border: 1px solid var(--border);
              border-radius: 12px; padding: 1.25rem; }

  .card-label { font-size: 0.75rem; color: var(--muted); font-weight: 500;
                text-transform: uppercase; letter-spacing: 0.05em; }

  .card-value { font-size: 2rem; font-weight: 700; margin-top: 0.5rem;
                background: linear-gradient(135deg, var(--accent), var(--accent2));
                -webkit-background-clip: text; -webkit-text-fill-color: transparent; }

  /* Tables */
  .section  { background: var(--surface); border: 1px solid var(--border);
              border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem; }

  .section-title { font-size: 1rem; font-weight: 600; margin-bottom: 1rem;
                   color: var(--text); }

  table     { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
  th        { text-align: left; padding: 0.5rem 0.75rem; color: var(--muted);
              font-weight: 500; font-size: 0.75rem; text-transform: uppercase;
              letter-spacing: 0.05em; border-bottom: 1px solid var(--border); }
  td        { padding: 0.75rem; border-bottom: 1px solid var(--border); }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(108,99,255,0.05); }

  .pill     { background: rgba(34,197,94,0.15); color: var(--green); padding: 0.2rem 0.6rem;
              border-radius: 9999px; font-size: 0.7rem; font-weight: 600; }

  .empty    { text-align: center; color: var(--muted); padding: 2rem;
              font-size: 0.875rem; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>📬 Cold Mail Tracker</h1>
    <div class="subtitle">Real-time email open tracking for your outreach campaign</div>
  </div>
  <div class="badge">Live</div>
</div>

<div class="cards">
  <div class="card">
    <div class="card-label">Total Opens</div>
    <div class="card-value">{{ total_opens }}</div>
  </div>
  <div class="card">
    <div class="card-label">Unique Openers</div>
    <div class="card-value">{{ unique_opens }}</div>
  </div>
  <div class="card">
    <div class="card-label">Companies</div>
    <div class="card-value">{{ companies }}</div>
  </div>
  <div class="card">
    <div class="card-label">Open Rate</div>
    <div class="card-value">{{ open_rate }}%</div>
  </div>
</div>

<div class="section">
  <div class="section-title">🕐 Recent Opens</div>
  {% if recent %}
  <table>
    <thead>
      <tr>
        <th>Email</th>
        <th>Company</th>
        <th>Opened At (UTC)</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
      {% for row in recent %}
      <tr>
        <td>{{ row.email }}</td>
        <td>{{ row.company }}</td>
        <td>{{ row.opened_at }}</td>
        <td><span class="pill">Opened</span></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">No opens tracked yet. Send some emails first!</div>
  {% endif %}
</div>

<div class="section">
  <div class="section-title">🏢 Opens by Company</div>
  {% if by_company %}
  <table>
    <thead>
      <tr>
        <th>Company</th>
        <th>Total Opens</th>
        <th>Unique Openers</th>
      </tr>
    </thead>
    <tbody>
      {% for row in by_company %}
      <tr>
        <td>{{ row.company }}</td>
        <td>{{ row.opens }}</td>
        <td>{{ row.unique_opens }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">No data yet.</div>
  {% endif %}
</div>

</body>
</html>"""


@app.route("/stats")
def stats():
    with get_db() as conn:
        total_opens   = conn.execute("SELECT COUNT(*) FROM opens").fetchone()[0]
        unique_opens  = conn.execute("SELECT COUNT(DISTINCT email) FROM opens").fetchone()[0]
        companies     = conn.execute("SELECT COUNT(DISTINCT company) FROM opens WHERE company != ''").fetchone()[0]
        recent        = conn.execute(
            "SELECT email, company, opened_at FROM opens ORDER BY opened_at DESC LIMIT 100"
        ).fetchall()
        by_company    = conn.execute(
            "SELECT company, COUNT(*) as opens, COUNT(DISTINCT email) as unique_opens "
            "FROM opens WHERE company != '' GROUP BY company ORDER BY opens DESC LIMIT 30"
        ).fetchall()

    open_rate = round((unique_opens / max(total_opens, 1)) * 100) if total_opens else 0

    return render_template_string(
        DASHBOARD,
        total_opens=total_opens,
        unique_opens=unique_opens,
        companies=companies,
        open_rate=open_rate,
        recent=recent,
        by_company=by_company,
    )


@app.route("/api/stats")
def api_stats():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT email, company, opened_at, ip FROM opens ORDER BY opened_at DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/")
def index():
    return "Cold Mail Tracker ✅ — visit /stats for the dashboard"


# ── Startup ───────────────────────────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
