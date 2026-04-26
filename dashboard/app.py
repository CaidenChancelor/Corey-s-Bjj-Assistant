import os
import json
import logging
import requests
from functools import wraps
from flask import Flask, render_template, request, redirect, session, url_for, send_from_directory, Response

from claude_tools import handle_chat_message

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET", "dev-secret-change-me")

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
BOT_URL = os.environ.get("BOT_URL", "https://corey-s-bjj-assistant-production.up.railway.app")
API_TOKEN = os.environ.get("API_TOKEN", "")

# Pre-load Corey's React bundle once at startup
APP_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "app.html")
try:
    with open(APP_HTML_PATH, "r") as f:
        APP_HTML = f.read()
except FileNotFoundError:
    APP_HTML = None
    logging.warning(f"app.html not found at {APP_HTML_PATH} — falling back to old templates")

def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

def fetch_bot_status():
    try:
        r = requests.get(
            f"{BOT_URL}/api/status",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            timeout=10,
        )
        return r.json() if r.ok else {"error": f"Bot returned {r.status_code}"}
    except Exception as e:
        return {"error": f"Couldn't reach bot: {e}"}

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if not DASHBOARD_PASSWORD:
            return render_template("login.html", error="DASHBOARD_PASSWORD not set on the server")
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("home"))
        return render_template("login.html", error="wrong password")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@require_login
def home():
    """Serve Corey's React design with live bot data injected via window.__bjjdata."""
    if APP_HTML is None:
        return redirect(url_for("dashboard_old"))
    status = fetch_bot_status()
    inject = (
        f'<script>window.__bjjdata = {json.dumps(status)};'
        f'window.__bjjeditor = {{endpoint: "/editor/send"}};</script>'
    )
    # Inject right before </head>
    html = APP_HTML.replace("</head>", inject + "</head>", 1)
    return Response(html, mimetype="text/html")

# ── Fallback (old) routes — kept accessible during Phase 1-3 ─────────────

@app.route("/dashboard-old")
@require_login
def dashboard_old():
    return render_template("dashboard.html", status=fetch_bot_status(), bot_url=BOT_URL)

@app.route("/editor-old")
@require_login
def editor_old():
    return render_template("editor.html", history=session.get("chat", []))

# ── Editor backend (works for both old UI and Phase 3 React wiring) ─────

@app.route("/editor/send", methods=["POST"])
@require_login
def editor_send():
    msg = request.form.get("message", "").strip()
    if not msg:
        # If this came from the old form-based UI, redirect back
        if request.headers.get("Accept", "").startswith("text/html"):
            return redirect(url_for("editor_old"))
        return {"error": "empty message"}, 400
    history = session.get("chat", [])
    reply = handle_chat_message(msg, history)
    history.append({"role": "user", "content": msg})
    history.append({"role": "assistant", "content": reply})
    session["chat"] = history[-40:]
    # Old form-based UI expects redirect, JSON callers expect JSON
    if request.headers.get("Accept", "").startswith("text/html"):
        return redirect(url_for("editor_old"))
    return {"reply": reply}

@app.route("/editor/clear", methods=["POST"])
@require_login
def editor_clear():
    session["chat"] = []
    if request.headers.get("Accept", "").startswith("text/html"):
        return redirect(url_for("editor_old"))
    return {"cleared": True}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
