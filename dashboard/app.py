import os
import json
import logging
import sqlite3 as _sqlite3
import requests
from functools import wraps
from flask import Flask, render_template, request, redirect, session, url_for, Response

from claude_tools import handle_chat_message, compact_editor_history

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET", "dev-secret-change-me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
BOT_URL = os.environ.get("BOT_URL", "https://corey-s-bjj-assistant-production.up.railway.app")
API_TOKEN = os.environ.get("API_TOKEN", "")

EDITOR_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "editor_history.db")

def _init_editor_db():
    with _sqlite3.connect(EDITOR_DB) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS editor_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            role      TEXT,
            content   TEXT,
            created_at TEXT
        )''')
        conn.commit()

_init_editor_db()

def _load_editor_history(limit=40):
    try:
        with _sqlite3.connect(EDITOR_DB) as conn:
            rows = conn.execute(
                'SELECT role, content FROM editor_history ORDER BY id DESC LIMIT ?', (limit,)
            ).fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    except Exception:
        return []

def _save_editor_message(role, content):
    try:
        from datetime import datetime as _dt
        with _sqlite3.connect(EDITOR_DB) as conn:
            conn.execute(
                'INSERT INTO editor_history (role, content, created_at) VALUES (?,?,?)',
                (role, content, _dt.now().isoformat())
            )
            conn.commit()
    except Exception as e:
        logging.error(f"Editor history save error: {e}")

def _clear_editor_history():
    try:
        with _sqlite3.connect(EDITOR_DB) as conn:
            conn.execute('DELETE FROM editor_history')
            conn.commit()
    except Exception as e:
        logging.error(f"Editor history clear error: {e}")

def _replace_editor_history(messages):
    """Replace all history with a compacted set of messages."""
    try:
        from datetime import datetime as _dt
        with _sqlite3.connect(EDITOR_DB) as conn:
            conn.execute('DELETE FROM editor_history')
            for msg in messages:
                conn.execute(
                    'INSERT INTO editor_history (role, content, created_at) VALUES (?,?,?)',
                    (msg["role"], msg["content"], _dt.now().isoformat())
                )
            conn.commit()
    except Exception as e:
        logging.error(f"Editor history replace error: {e}")

# Pre-load the React bundle once at startup
APP_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "app.html")
try:
    with open(APP_HTML_PATH, "r") as f:
        APP_HTML = f.read()
except FileNotFoundError:
    APP_HTML = None
    logging.warning(f"app.html not found at {APP_HTML_PATH} — falling back to Jinja templates")


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
            timeout=5,
        )
        return r.json() if r.ok else {"error": f"Bot returned {r.status_code}"}
    except Exception as e:
        return {"error": f"Couldn't reach bot: {e}"}


def serve_react_bundle():
    """Serve Corey's React design with live bot data injected via window.__bjjdata."""
    status = fetch_bot_status()
    inject = (
        f'<script>window.__bjjdata = {json.dumps(status)};'
        f'window.__bjjeditor = {{endpoint: "/editor/send"}};</script>'
    )
    html = APP_HTML.replace("</head>", inject + "</head>", 1)
    return Response(html, mimetype="text/html")


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
    if APP_HTML is None:
        return redirect(url_for("dashboard_old"))
    return serve_react_bundle()


# /editor URL serves the same React bundle — internal NavPill handles tab switching
@app.route("/editor")
@require_login
def editor():
    if APP_HTML is None:
        return redirect(url_for("editor_old"))
    return serve_react_bundle()


# ── Working Jinja fallbacks (real bot data, plain UI) ─────────────────────

@app.route("/dashboard-old")
@require_login
def dashboard_old():
    return render_template("dashboard.html", status=fetch_bot_status(), bot_url=BOT_URL)


@app.route("/editor-old")
@require_login
def editor_old():
    return render_template("editor.html", history=session.get("chat", []))


# ── Editor backend (used by Jinja fallback today; React bundle in Phase 3) ─

@app.route("/editor/send", methods=["POST"])
@require_login
def editor_send():
    if request.is_json:
        msg = (request.get_json(force=True) or {}).get("message", "").strip()
    else:
        msg = request.form.get("message", "").strip()

    if not msg:
        if request.is_json:
            return {"error": "empty"}, 400
        return redirect(url_for("editor"))

    # Load history from DB
    history = _load_editor_history(40)

    # Auto-compact if getting long
    if len(history) >= 30:
        compacted = compact_editor_history(history)
        if compacted:
            _replace_editor_history(compacted)
            history = compacted

    # Save user message to DB
    _save_editor_message("user", msg)

    # Call Claude
    result = handle_chat_message(msg, history)
    if isinstance(result, dict):
        reply = result.get("reply", "")
        tool_events = result.get("tool_events", [])
    else:
        reply = result
        tool_events = []

    # Save assistant reply to DB
    _save_editor_message("assistant", reply)

    if request.is_json:
        return {"reply": reply, "tool_events": tool_events}
    return redirect(url_for("editor"))


@app.route("/editor/clear", methods=["POST"])
@require_login
def editor_clear():
    _clear_editor_history()
    if request.is_json:
        return {"ok": True}
    return redirect(url_for("editor"))


@app.route("/editor/history", methods=["GET"])
@require_login
def editor_history_route():
    messages = _load_editor_history(40)
    return {"messages": messages}


# ── Bot API proxy — forwards to bot service with shared token ──────────────

def _bot(method, path, json_body=None):
    """Forward a request to the bot API with the shared Bearer token."""
    try:
        r = requests.request(
            method,
            f"{BOT_URL}{path}",
            headers={"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"},
            json=json_body,
            timeout=8,
        )
        try:
            return r.json(), r.status_code
        except ValueError:
            return {"error": f"Bot returned non-JSON (HTTP {r.status_code})"}, 502
    except Exception as e:
        return {"error": str(e)}, 502


# Meals
@app.route("/api/meals", methods=["POST"])
@require_login
def proxy_meals_create():
    body, status = _bot("POST", "/api/meals", request.get_json(force=True))
    return body, status


@app.route("/api/meals/<int:meal_id>", methods=["PATCH", "DELETE"])
@require_login
def proxy_meals_item(meal_id):
    body, status = _bot(
        request.method,
        f"/api/meals/{meal_id}",
        request.get_json(force=True) if request.method == "PATCH" else None,
    )
    return body, status


# Injuries
@app.route("/api/injuries", methods=["POST"])
@require_login
def proxy_injuries_create():
    body, status = _bot("POST", "/api/injuries", request.get_json(force=True))
    return body, status


@app.route("/api/injuries/<int:injury_id>", methods=["PATCH", "DELETE"])
@require_login
def proxy_injuries_item(injury_id):
    body, status = _bot(
        request.method,
        f"/api/injuries/{injury_id}",
        request.get_json(force=True) if request.method == "PATCH" else None,
    )
    return body, status


# Allergies
@app.route("/api/allergies", methods=["POST"])
@require_login
def proxy_allergies_create():
    body, status = _bot("POST", "/api/allergies", request.get_json(force=True))
    return body, status


@app.route("/api/allergies/<int:allergy_id>", methods=["PATCH", "DELETE"])
@require_login
def proxy_allergies_item(allergy_id):
    body, status = _bot(
        request.method,
        f"/api/allergies/{allergy_id}",
        request.get_json(force=True) if request.method == "PATCH" else None,
    )
    return body, status


# Problems
@app.route("/api/problems", methods=["POST"])
@require_login
def proxy_problems_create():
    body, status = _bot("POST", "/api/problems", request.get_json(force=True))
    return body, status


@app.route("/api/problems/<int:problem_id>", methods=["PATCH", "DELETE"])
@require_login
def proxy_problems_item(problem_id):
    body, status = _bot(
        request.method,
        f"/api/problems/{problem_id}",
        request.get_json(force=True) if request.method == "PATCH" else None,
    )
    return body, status


# Water
@app.route("/api/water/add", methods=["POST"])
@require_login
def proxy_water_add():
    body, status = _bot("POST", "/api/water/add", request.get_json(force=True))
    return body, status


@app.route("/api/water/entry/<int:entry_id>", methods=["DELETE"])
@require_login
def proxy_water_delete(entry_id):
    body, status = _bot("DELETE", f"/api/water/entry/{entry_id}")
    return body, status


# Technique history
@app.route("/api/technique-history", methods=["GET"])
@require_login
def proxy_technique_history():
    body, status = _bot("GET", "/api/technique-history")
    return body, status


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
