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
        # Logs actual email opens
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
        # Logs every sent email
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sends (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                email       TEXT    NOT NULL UNIQUE,
                company     TEXT    DEFAULT '',
                sent_at     TEXT    NOT NULL,
                status      TEXT    DEFAULT 'sent',
                bounce_reason TEXT  DEFAULT ''
            )
        """)
        # Migrate existing DBs that don't have the status or bounce_reason columns yet
        try:
            conn.execute("ALTER TABLE sends ADD COLUMN status TEXT DEFAULT 'sent'")
        except sqlite3.OperationalError:
            pass # column already exists
        try:
            conn.execute("ALTER TABLE sends ADD COLUMN bounce_reason TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass # column already exists
        conn.commit()


# ── Tracking API to record sends ─────────────────────────────────────────────
@app.route("/api/log_send", methods=["POST"])
def log_send():
    data = request.get_json() or {}
    email = data.get("email")
    company = data.get("company", "")
    sent_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    if not email:
        return jsonify({"error": "Missing email"}), 400

    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO sends (email, company, sent_at, status, bounce_reason) VALUES (?, ?, ?, 'sent', '') "
                "ON CONFLICT(email) DO UPDATE SET company=excluded.company, sent_at=excluded.sent_at, status='sent', bounce_reason=''",
                (email, company, sent_at)
            )
            conn.commit()
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Tracking API to record bounces ───────────────────────────────────────────
@app.route("/api/log_bounce", methods=["POST"])
def log_bounce():
    data = request.get_json() or {}
    email = data.get("email")
    reason = data.get("reason", "Bounce — invalid address")

    if not email:
        return jsonify({"error": "Missing email"}), 400

    try:
        with get_db() as conn:
            # Mark it as bounced
            conn.execute(
                "UPDATE sends SET status='bounced', bounce_reason=? WHERE email=?",
                (reason, email)
            )
            # Create a placeholder in sends if it somehow wasn't logged during send
            conn.execute(
                "INSERT OR IGNORE INTO sends (email, status, bounce_reason, sent_at) VALUES (?, 'bounced', ?, ?)",
                (email, reason, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn.commit()
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
            # First, ensure the email is recorded as sent (fallback if log_send missed it)
            conn.execute(
                "INSERT OR IGNORE INTO sends (email, company, sent_at) VALUES (?, ?, ?)",
                (email, company, opened_at)
            )
            # Log the open
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
    --blue:    #3b82f6;
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

  /* Search & Actions */
  .toolbar {
    margin-bottom: 1.5rem;
  }
  .search-input {
    width: 100%;
    max-width: 400px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.75rem 1rem;
    color: var(--text);
    font-family: inherit;
    font-size: 0.875rem;
  }
  .search-input:focus {
    outline: none;
    border-color: var(--accent);
  }

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

  /* Pills */
  .pill-opened { background: rgba(59,130,246,0.15); color: var(--blue); padding: 0.2rem 0.6rem;
                 border-radius: 9999px; font-size: 0.7rem; font-weight: 600; }
  .pill-sent   { background: rgba(34,197,94,0.15); color: var(--green); padding: 0.2rem 0.6rem;
                 border-radius: 9999px; font-size: 0.7rem; font-weight: 600; }
  .pill-bounced { background: rgba(239,68,68,0.15); color: var(--red); padding: 0.2rem 0.6rem;
                  border-radius: 9999px; font-size: 0.7rem; font-weight: 600; }

  .empty    { text-align: center; color: var(--muted); padding: 2rem;
              font-size: 0.875rem; }
</style>
<script>
  function filterTable() {
    const query = document.getElementById('search').value.toLowerCase();
    const rows = document.querySelectorAll('#email-table tbody tr');
    rows.forEach(row => {
      const email = row.cells[0].textContent.toLowerCase();
      const company = row.cells[1].textContent.toLowerCase();
      if (email.includes(query) || company.includes(query)) {
        row.style.display = '';
      } else {
        row.style.display = 'none';
      }
    });
  }
</script>
</head>
<body>

<div class="header">
  <div>
    <h1>📬 Cold Mail Tracker</h1>
    <div class="subtitle">Real-time email outreach status and open tracking</div>
  </div>
  <div class="badge">Live</div>
</div>

<div class="cards">
  <div class="card">
    <div class="card-label">Total Sent</div>
    <div class="card-value">{{ total_sent }}</div>
  </div>
  <div class="card">
    <div class="card-label">Total Opens</div>
    <div class="card-value">{{ total_opens }}</div>
  </div>
  <div class="card">
    <div class="card-label">Total Bounces</div>
    <div class="card-value" style="background: linear-gradient(135deg, #ef4444, #f97316); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">{{ total_bounces }}</div>
  </div>
  <div class="card">
    <div class="card-label">Open Rate</div>
    <div class="card-value">{{ open_rate }}%</div>
  </div>
</div>

<div class="toolbar">
  <input type="text" id="search" onkeyup="filterTable()" class="search-input" placeholder="Search by email or company...">
</div>

<div class="section">
  <div class="section-title">📧 Outreach Status</div>
  {% if outreach %}
  <table id="email-table">
    <thead>
      <tr>
        <th>Email</th>
        <th>Company</th>
        <th>Sent At (UTC)</th>
        <th>Last Opened (UTC)</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
      {% for row in outreach %}
      <tr>
        <td>{{ row.email }}</td>
        <td>{{ row.company }}</td>
        <td>{{ row.sent_at }}</td>
        <td>{{ row.opened_at if row.opened_at else '-' }}</td>
        <td>
          {% if row.status == 'bounced' %}
            <span class="pill-bounced" title="{{ row.bounce_reason }}">Bounced</span>
          {% elif row.opens_count and row.opens_count > 0 %}
            <span class="pill-opened">Opened ({{ row.opens_count }})</span>
          {% else %}
            <span class="pill-sent">Sent</span>
          {% endif %}
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
        total_sent    = conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0]
        total_opens   = conn.execute("SELECT COUNT(*) FROM opens").fetchone()[0]
        total_bounces = conn.execute("SELECT COUNT(*) FROM sends WHERE status='bounced'").fetchone()[0]
        unique_opens  = conn.execute("SELECT COUNT(DISTINCT email) FROM opens").fetchone()[0]

        # Select all sent emails and join with open stats
        outreach = conn.execute("""
            SELECT 
                s.email,
                s.company,
                s.sent_at,
                s.status,
                s.bounce_reason,
                (SELECT COUNT(*) FROM opens o WHERE o.email = s.email) as opens_count,
                (SELECT MAX(opened_at) FROM opens o WHERE o.email = s.email) as opened_at
            FROM sends s
            ORDER BY s.sent_at DESC
        """).fetchall()

    open_rate = round((unique_opens / max(total_sent, 1)) * 100) if total_sent else 0

    return render_template_string(
        DASHBOARD,
        total_sent=total_sent,
        total_opens=total_opens,
        total_bounces=total_bounces,
        unique_opens=unique_opens,
        open_rate=open_rate,
        outreach=outreach,
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
