import os
import logging
import requests
from functools import wraps
from flask import Flask, render_template, request, redirect, session, url_for

from claude_tools import handle_chat_message

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET", "dev-secret-change-me")

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
BOT_URL = os.environ.get("BOT_URL", "https://corey-s-bjj-assistant-production.up.railway.app")
API_TOKEN = os.environ.get("API_TOKEN", "")


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
    return render_template("dashboard.html", status=fetch_bot_status(), bot_url=BOT_URL)


@app.route("/editor")
@require_login
def editor():
    return render_template("editor.html", history=session.get("chat", []))


@app.route("/editor/send", methods=["POST"])
@require_login
def editor_send():
    msg = request.form.get("message", "").strip()
    if not msg:
        return redirect(url_for("editor"))
    history = session.get("chat", [])
    reply = handle_chat_message(msg, history)
    history.append({"role": "user", "content": msg})
    history.append({"role": "assistant", "content": reply})
    session["chat"] = history[-40:]
    return redirect(url_for("editor"))


@app.route("/editor/clear", methods=["POST"])
@require_login
def editor_clear():
    session["chat"] = []
    return redirect(url_for("editor"))


# Legacy URL aliases — redirect to the current pages
@app.route("/dashboard-old")
def dashboard_old():
    return redirect(url_for("home"))


@app.route("/editor-old")
def editor_old():
    return redirect(url_for("editor"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
