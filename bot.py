import os
import re
import json
import base64
import sqlite3
import threading
import requests as req
from datetime import datetime, timedelta
import pytz
from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from apscheduler.schedulers.background import BackgroundScheduler
import anthropic
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
AUTH_TOKEN  = os.environ.get('TWILIO_AUTH_TOKEN')
FROM_NUMBER = os.environ.get('FROM_NUMBER', 'whatsapp:+14155238886')
MY_NUMBER   = os.environ.get('MY_NUMBER',   'whatsapp:+13054601000')

# Drilling partners
PARTNERS = {
    "God-Killer": "whatsapp:+19544173000",
}

if not ACCOUNT_SID or not AUTH_TOKEN:
    logging.warning("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set — bot will not send messages")
client = Client(ACCOUNT_SID, AUTH_TOKEN)
CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME', '')
CLOUDINARY_API_KEY    = os.environ.get('CLOUDINARY_API_KEY', '')
CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET', '')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None
CLAUDE_MODEL = "claude-sonnet-4-6"
WATER_GOAL_L = 3.0
CALORIE_GOAL = int(os.environ.get('CALORIE_GOAL', 2800))
TZ = pytz.timezone('America/New_York')
DB_PATH = os.environ.get('DB_PATH', '/data/bjj.db')

# Chat history for conversational context
chat_history = []

# ── JOURNAL (SQLite) ───────────────────────────────────────────────────────────

def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('PRAGMA journal_mode=WAL')  # allow concurrent reader+writer threads
        conn.execute('''CREATE TABLE IF NOT EXISTS journal (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            date       TEXT,
            session    TEXT,
            notes      TEXT,
            created_at TEXT
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            role       TEXT,
            content    TEXT,
            created_at TEXT
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS daily_water (
            date   TEXT PRIMARY KEY,
            liters REAL
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS meals (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            date       TEXT,
            time       TEXT,
            name       TEXT,
            calories   INTEGER,
            kind       TEXT,
            notes      TEXT,
            created_at TEXT
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS injuries (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT,
            body_part     TEXT,
            severity      TEXT,
            notes         TEXT,
            resolved      INTEGER DEFAULT 0,
            partner       TEXT,
            when_happened TEXT,
            created_at    TEXT
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS allergies (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT,
            time            TEXT,
            severity        TEXT,
            category        TEXT,
            trigger_name    TEXT,
            symptoms        TEXT,
            medication      TEXT,
            training_impact TEXT,
            missed_training INTEGER DEFAULT 0,
            created_at      TEXT
        )''')
        try:
            conn.execute('ALTER TABLE injuries ADD COLUMN partner TEXT')
        except Exception: pass
        try:
            conn.execute('ALTER TABLE injuries ADD COLUMN when_happened TEXT')
        except Exception: pass
        try:
            conn.execute('ALTER TABLE allergies ADD COLUMN trigger_name TEXT')
        except Exception: pass
        try:
            conn.execute(
                'UPDATE allergies SET trigger_name = "trigger" '
                'WHERE trigger_name IS NULL AND "trigger" IS NOT NULL'
            )
        except Exception: pass
        conn.execute('''CREATE TABLE IF NOT EXISTS water_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            date       TEXT,
            time       TEXT,
            amount_l   REAL,
            created_at TEXT
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS problems (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,
            tier        TEXT DEFAULT 'med',
            description TEXT DEFAULT '',
            resolved    INTEGER DEFAULT 0,
            created_at  TEXT
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS techniques (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE,
    summary    TEXT,
    created_at TEXT
)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS technique_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    technique_id INTEGER,
    date         TEXT,
    session      TEXT,
    notes        TEXT,
    sentiment    TEXT,
    video_url    TEXT,
    created_at   TEXT
)''')
        try:
            conn.execute('ALTER TABLE technique_log ADD COLUMN video_url TEXT')
        except Exception:
            pass  # column already exists
        try:
            conn.execute('ALTER TABLE techniques ADD COLUMN summary TEXT')
        except Exception:
            pass
        conn.commit()

def save_message(role, content):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                'INSERT INTO messages (role, content, created_at) VALUES (?,?,?)',
                (role, content, datetime.now(TZ).isoformat())
            )
            conn.commit()
    except Exception as e:
        logging.error(f"Message save error: {e}")

def load_chat_history(n=20):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT role, content FROM messages ORDER BY created_at DESC LIMIT ?', (n,)
            ).fetchall()
            return [{"role": r, "content": c} for r, c in reversed(rows)]
    except Exception:
        return []

def save_water_to_db():
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                'INSERT OR REPLACE INTO daily_water (date, liters) VALUES (?,?)',
                (today, state["water_today"])
            )
            conn.commit()
    except Exception as e:
        logging.error(f"Water DB save error: {e}")

def load_water_from_db():
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                'SELECT liters FROM daily_water WHERE date = ?', (today,)
            ).fetchone()
            if row:
                state["water_today"] = row[0]
                state["water_date"] = today
    except Exception as e:
        logging.error(f"Water DB load error: {e}")

def save_journal_entry(session, notes):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                'INSERT INTO journal (date, session, notes, created_at) VALUES (?,?,?,?)',
                (datetime.now(TZ).strftime('%Y-%m-%d'), session, notes, datetime.now(TZ).isoformat())
            )
            conn.commit()
        logging.info(f"JOURNAL [{session}]: {notes[:80]}")
    except Exception as e:
        logging.error(f"Journal save error: {e}")

def extract_issue(notes):
    """If the note mentions struggling with a technique, return the technique name. Else None."""
    if not claude:
        return None
    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=20,
            system="You are a classifier. Extract the technique name if the person mentions struggling or having an issue with it. Reply with ONLY the technique name (e.g. '50-50', 'butterfly guard'). If no struggle is mentioned, reply NO.",
            messages=[{"role": "user", "content": notes}]
        )
        result = response.content[0].text.strip()
        return None if result.upper() == "NO" else result
    except Exception:
        return None


def interpret_debrief_reply(step, user_reply, context=None):
    """Use Claude to intelligently interpret a debrief message.
    Returns dict with one of: understood/correction/skip/unclear keys."""
    if not claude:
        return {"understood": True, "value": user_reply.strip()}
    ctx = f"\nContext (previous answers): {json.dumps(context)}" if context else ""
    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=120,
            system=(
                "You interpret replies in a BJJ training debrief. "
                "Return JSON only — one of these shapes:\n"
                '{"understood": true, "value": "extracted clean value"}\n'
                '{"correction": true, "field": "headline", "value": "corrected technique name"} — user is fixing a previous answer\n'
                '{"skip": true} — user wants to skip\n'
                '{"unclear": true, "ask": "one short clarifying question"}\n'
                "For the headline step: extract just the technique name (e.g. 'spider lasso') from natural language like 'we worked on spider lasso today'.\n"
                "For corrections: detect phrases like 'i meant', 'i mean', 'actually', 'wait', 'no i said', 'correction'.\n"
                "For skip: detect 'skip', 'no', 'nah', 'nothing', 'n/a'.\n"
                "Return ONLY valid JSON."
            ),
            messages=[{"role": "user", "content": f"Step: {step}\nUser reply: {user_reply}{ctx}"}],
        )
        raw = re.sub(r'^```\w*\s*|\s*```$', '', response.content[0].text.strip()).strip()
        return json.loads(raw)
    except Exception:
        return {"understood": True, "value": user_reply.strip()}


def classify_message(text):
    """One Claude call → structured intents.

    Returns a dict with these top-level keys:
      - water_l: float | None — user logging water intake
      - meal: {name, calories, kind} | None — user logging a meal
      - injury: {body_part, severity, notes} | None — user reporting an injury
      - allergy: bool — user reporting an allergy flare-up / symptoms
      - bruno_lesson: bool — user spontaneously talking about a Bruno private/class
      - problem: {position, hint} | None — user mentioning a technique they're stuck on
    """
    empty = {
        "water_l": None, "meal": None, "injury": None,
        "allergy": False, "bruno_lesson": False, "problem": None,
    }
    if not claude or not text:
        return empty
    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=320,
            system=(
                "You are an intent classifier for a BJJ training assistant. "
                "Given a user message, output JSON with these fields:\n"
                '- water_l: liters of water if user is logging water intake, else null. '
                'Examples: "drank 1L"→1.0, "had 500ml"→0.5, "just finished a glass"→0.25, '
                '"chugged a bottle"→0.5, "drank 2 bottles of water"→1.0.\n'
                '- meal: object {name, calories, kind} if user is logging a meal/food, else null. '
                'kind ∈ {"breakfast","lunch","dinner","snack","pre-training","post-training","other"}. '
                'Estimate calories (integer) if not given.\n'
                '- injury: object {body_part, severity, notes} if user is reporting a NEW injury, tweak, or pain, else null. '
                'severity ∈ {"minor","moderate","severe"}. Body part should be specific (e.g. "left knee", "lower back"). '
                'Only set when there is clear injury intent — not when they mention an old/known injury in passing.\n'
                '- allergy: true if user is reporting allergy symptoms or a flare-up (sneezing, itchy eyes, hives, asthma, '
                'allergic reaction, anaphylaxis, "allergies acting up", "stuffed up", congestion from allergies, etc.). '
                'Otherwise false.\n'
                '- bruno_lesson: true if the user is spontaneously talking about a private session with Bruno '
                '(e.g. "had a sick private today", "bruno worked me hard", "private felt great", "got the most out of bruno today"). '
                'NOT true for general training talk or other classes.\n'
                '- problem: object {position, hint} if the user is mentioning a SPECIFIC technique or position they keep '
                'getting stuck on / struggle with / want flagged. position = the technique name, hint = a short summary '
                'of the issue if they gave one. Examples: "I keep getting stuck in half guard"→{position:"half guard",hint:"keeps getting stuck"}, '
                '"spider lasso has been killing me"→{position:"spider lasso",hint:"struggling"}. '
                'Else null. Do NOT set this just because they mention a technique in passing.\n\n'
                "Set at most one of {allergy, bruno_lesson, problem, injury} to a positive value — pick the strongest signal. "
                "Return ONLY valid JSON. No markdown, no explanation."
            ),
            messages=[{"role": "user", "content": text}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r'^```\w*\s*|\s*```$', '', raw).strip()
        data = json.loads(raw)
        return {
            "water_l": data.get("water_l"),
            "meal": data.get("meal"),
            "injury": data.get("injury"),
            "allergy": bool(data.get("allergy")),
            "bruno_lesson": bool(data.get("bruno_lesson")),
            "problem": data.get("problem"),
        }
    except Exception as e:
        logging.error(f"classify_message error: {e}")
        return empty


def save_meal(name, calories, kind="other", notes=""):
    now = datetime.now(TZ)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                'INSERT INTO meals (date, time, name, calories, kind, notes, created_at) VALUES (?,?,?,?,?,?,?)',
                (now.strftime('%Y-%m-%d'), now.strftime('%H:%M'), name, int(calories or 0), kind, notes, now.isoformat()),
            )
            conn.commit()
        logging.info(f"MEAL: {name} ({calories} cal, {kind})")
    except Exception as e:
        logging.error(f"Meal save error: {e}")


def get_today_calories():
    today = datetime.now(TZ).strftime('%Y-%m-%d')
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute('SELECT COALESCE(SUM(calories), 0) FROM meals WHERE date = ?', (today,)).fetchone()
            return int(row[0] or 0)
    except Exception:
        return 0


def get_recent_meal():
    today = datetime.now(TZ).strftime('%Y-%m-%d')
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                'SELECT name, calories, time, kind FROM meals WHERE date = ? ORDER BY created_at DESC LIMIT 1',
                (today,),
            ).fetchone()
            return {"name": row[0], "calories": row[1], "time": row[2], "kind": row[3]} if row else None
    except Exception:
        return None


def get_all_meals_today():
    """Return all meals logged today for the dashboard Meals page."""
    today = datetime.now(TZ).strftime('%Y-%m-%d')
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT id, time, name, calories, kind, notes FROM meals WHERE date = ? ORDER BY time ASC',
                (today,)
            ).fetchall()
        return [{"id": r[0], "time": r[1], "name": r[2], "calories": r[3], "kind": r[4], "notes": r[5]} for r in rows]
    except Exception:
        return []


def save_injury(body_part, severity, notes):
    now = datetime.now(TZ)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                'INSERT INTO injuries (date, body_part, severity, notes, created_at) VALUES (?,?,?,?,?)',
                (now.strftime('%Y-%m-%d'), body_part, severity, notes, now.isoformat()),
            )
            conn.commit()
        logging.info(f"INJURY: {body_part} ({severity})")
    except Exception as e:
        logging.error(f"Injury save error: {e}")


def save_allergy(trigger, symptoms, severity="mild", category="general",
                 medication=None, training_impact="none", missed_training=False, notes=None):
    """Insert an allergy log into the allergies table."""
    now = datetime.now(TZ)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                'INSERT INTO allergies (date, time, severity, category, trigger_name, symptoms, medication, training_impact, missed_training, notes, created_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (
                    now.strftime('%Y-%m-%d'),
                    now.strftime('%H:%M'),
                    (severity or 'mild').strip().lower(),
                    (category or 'general').strip().lower(),
                    (trigger or '').strip(),
                    (symptoms or '').strip(),
                    (medication or '').strip() or None,
                    (training_impact or 'none').strip().lower(),
                    1 if missed_training else 0,
                    (notes or '').strip() or None,
                    now.isoformat(),
                ),
            )
            conn.commit()
        logging.info(f"ALLERGY: {trigger} ({severity})")
    except Exception as e:
        logging.error(f"Allergy save error: {e}")


def map_body_region(body_part_text):
    """Map a body part description to a Micro-Injuries region code."""
    text = body_part_text.lower()
    is_left = "left" in text
    is_right = "right" in text
    if "knee" in text:
        return "knee-l" if is_left else "knee-r"
    if "shoulder" in text:
        return "shoulder-l" if is_left else "shoulder-r"
    if "ankle" in text:
        return "ankle-l" if is_left else "ankle-r"
    if "elbow" in text:
        return "elbow-l" if is_left else "elbow-r"
    if "wrist" in text:
        return "wrist-l" if is_left else "wrist-r"
    if "hip" in text:
        return "hip-l" if is_left else "hip-r"
    if "rib" in text:
        return "ribs-l" if is_left else "ribs-r"
    if "neck" in text:
        return "neck"
    if "head" in text:
        return "head"
    if "chest" in text:
        return "chest"
    if "back" in text:
        return "lower-back"
    return "other"


def get_active_injuries():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT body_part, severity, notes, date FROM injuries WHERE resolved = 0 ORDER BY created_at DESC LIMIT 5'
            ).fetchall()
            return [{"body_part": r[0], "severity": r[1], "notes": r[2], "date": r[3]} for r in rows]
    except Exception:
        return []


def get_all_injuries():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT id, date, body_part, severity, notes, resolved, partner, when_happened, created_at '
                'FROM injuries ORDER BY created_at DESC LIMIT 20'
            ).fetchall()
        return [{"id": r[0], "date": r[1], "body_part": r[2], "severity": r[3],
                 "notes": r[4], "resolved": bool(r[5]), "partner": r[6],
                 "when_happened": r[7], "created_at": r[8]} for r in rows]
    except Exception:
        return []


def _infer_injury_time_bucket(when_happened, created_at):
    """Map an injury to one of three session buckets: morning / afternoon / evening.

    Night/late text and overnight timestamps fold into "evening" (night class
    window) — Corey only wants three categories on the dashboard.

    Precedence:
      1. evening words (evening/class/competition/rolls/open mat/night/late) or 5 PM onward
      2. afternoon words (afternoon/private/bruno/lunch/midday/noon) or 12-4 PM
      3. morning words (morning/drilling/strength/s&c) or 1-11 AM
    "am"/"pm" only count when attached to a digit.
    """
    text = (when_happened or "").lower().strip()
    if text:
        # Night/late + evening words + 5 PM onward all fold to "evening"
        if re.search(r"\b(night|late|evening|class|competition|rolls|open\s*mat)\b", text):
            return "evening"
        if re.search(r"\b([5-9]|1[01])(?::[0-5]\d)?\s*pm\b", text):
            return "evening"
        if re.search(r"\b(afternoon|private|bruno|lunch|midday|noon)\b", text):
            return "afternoon"
        if re.search(r"\b(12|[1-4])(?::[0-5]\d)?\s*pm\b", text):
            return "afternoon"
        if re.search(r"\b(morning|drilling|strength)\b", text) or "s&c" in text:
            return "morning"
        if re.search(r"\b(1[0-1]|[1-9])(?::[0-5]\d)?\s*am\b", text):
            return "morning"
        # 12 AM (overnight) → evening (closest session window)
        if re.search(r"\b12(?::[0-5]\d)?\s*am\b", text):
            return "evening"
    try:
        hour = datetime.fromisoformat(created_at).hour
        if 5 <= hour < 12:
            return "morning"
        if 12 <= hour < 17:
            return "afternoon"
        # 5 PM onward (including overnight) folds into evening
        return "evening"
    except Exception:
        return None


def get_injury_stats():
    """Stats for dashboard: top partners, body parts, and when injuries happen."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            partner_rows = conn.execute(
                "SELECT TRIM(partner), COUNT(*) as cnt FROM injuries "
                "WHERE partner IS NOT NULL AND TRIM(partner) != '' AND LOWER(TRIM(partner)) != 'solo' "
                "GROUP BY LOWER(TRIM(partner)) ORDER BY cnt DESC LIMIT 5"
            ).fetchall()
            body_rows = conn.execute(
                "SELECT TRIM(body_part), COUNT(*) as cnt FROM injuries "
                "WHERE body_part IS NOT NULL AND TRIM(body_part) != '' "
                "GROUP BY LOWER(TRIM(body_part)) ORDER BY cnt DESC LIMIT 5"
            ).fetchall()
            time_rows = conn.execute(
                "SELECT when_happened, created_at FROM injuries WHERE created_at IS NOT NULL OR when_happened IS NOT NULL"
            ).fetchall()
            time_buckets = {"morning": 0, "afternoon": 0, "evening": 0}
            for when_happened, created_at in time_rows:
                bucket = _infer_injury_time_bucket(when_happened, created_at)
                if bucket:
                    time_buckets[bucket] += 1
            total = conn.execute("SELECT COUNT(*) FROM injuries").fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM injuries WHERE resolved = 0").fetchone()[0]
            resolved = conn.execute("SELECT COUNT(*) FROM injuries WHERE resolved = 1").fetchone()[0]
        return {
            "total": total,
            "active": active,
            "resolved": resolved,
            "top_partners": [{"name": r[0], "count": r[1]} for r in partner_rows],
            "top_body_parts": [{"name": r[0], "count": r[1]} for r in body_rows],
            "time_buckets": time_buckets,
        }
    except Exception:
        return {"total": 0, "active": 0, "resolved": 0, "top_partners": [], "top_body_parts": [], "time_buckets": {"morning": 0, "afternoon": 0, "evening": 0}}


def _infer_allergy_time_bucket(time_text, created_at):
    text = (time_text or "").lower().strip()
    if text:
        if re.search(r"\b(night|late)\b", text):
            return "night"
        if re.search(r"\b(evening|class|training|rolls|open\s*mat)\b", text):
            return "evening"
        if re.search(r"\b([5-9])(?::[0-5]\d)?\s*pm\b", text):
            return "evening"
        if re.search(r"\b(10|11)(?::[0-5]\d)?\s*pm\b", text):
            return "night"
        if re.search(r"\b(afternoon|lunch|midday|noon)\b", text):
            return "afternoon"
        if re.search(r"\b(12|[1-4])(?::[0-5]\d)?\s*pm\b", text):
            return "afternoon"
        if re.search(r"\b(morning|wake|breakfast)\b", text):
            return "morning"
        if re.search(r"\b(1[0-1]|[1-9])(?::[0-5]\d)?\s*am\b", text):
            return "morning"
        if re.search(r"\b12(?::[0-5]\d)?\s*am\b", text):
            return "night"
        if re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", text):
            hour = int(re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", text).group(1))
            if 5 <= hour < 12:
                return "morning"
            if 12 <= hour < 17:
                return "afternoon"
            if 17 <= hour < 22:
                return "evening"
            return "night"
    try:
        hour = datetime.fromisoformat(created_at).hour
        if 5 <= hour < 12:
            return "morning"
        if 12 <= hour < 17:
            return "afternoon"
        if 17 <= hour < 22:
            return "evening"
        return "night"
    except Exception:
        return None


def get_all_allergies(limit=30):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT id, date, time, severity, category, trigger_name, symptoms, medication, '
                'training_impact, missed_training, created_at '
                'FROM allergies ORDER BY date DESC, created_at DESC LIMIT ?',
                (limit,)
            ).fetchall()
        return [
            {
                "id": r[0],
                "date": r[1],
                "time": r[2],
                "severity": r[3],
                "category": r[4],
                "trigger": r[5],
                "symptoms": r[6],
                "medication": r[7],
                "training_impact": r[8],
                "missed_training": bool(r[9]),
                "created_at": r[10],
            }
            for r in rows
        ]
    except Exception:
        return []


def get_allergy_stats():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            total = conn.execute("SELECT COUNT(*) FROM allergies").fetchone()[0]
            since_7 = (datetime.now(TZ) - timedelta(days=7)).strftime('%Y-%m-%d')
            last_7_days = conn.execute("SELECT COUNT(*) FROM allergies WHERE date >= ?", (since_7,)).fetchone()[0]
            missed_training = conn.execute("SELECT COUNT(*) FROM allergies WHERE missed_training = 1").fetchone()[0]
            trigger_rows = conn.execute(
                "SELECT LOWER(TRIM(trigger_name)), COUNT(*) FROM allergies "
                "WHERE trigger_name IS NOT NULL AND TRIM(trigger_name) != '' "
                "GROUP BY LOWER(TRIM(trigger_name)) ORDER BY COUNT(*) DESC LIMIT 5"
            ).fetchall()
            category_rows = conn.execute(
                "SELECT COALESCE(NULLIF(TRIM(category), ''), 'general'), COUNT(*) FROM allergies "
                "GROUP BY LOWER(COALESCE(NULLIF(TRIM(category), ''), 'general')) ORDER BY COUNT(*) DESC"
            ).fetchall()
            severity_rows = conn.execute(
                "SELECT COALESCE(NULLIF(TRIM(severity), ''), 'mild'), COUNT(*) FROM allergies "
                "GROUP BY LOWER(COALESCE(NULLIF(TRIM(severity), ''), 'mild'))"
            ).fetchall()
            impact_rows = conn.execute(
                "SELECT COALESCE(NULLIF(TRIM(training_impact), ''), 'none'), COUNT(*) FROM allergies "
                "GROUP BY LOWER(COALESCE(NULLIF(TRIM(training_impact), ''), 'none'))"
            ).fetchall()
            time_rows = conn.execute("SELECT time, created_at FROM allergies").fetchall()
        time_buckets = {"morning": 0, "afternoon": 0, "evening": 0}
        for time_text, created_at in time_rows:
            bucket = _infer_allergy_time_bucket(time_text, created_at)
            if bucket:
                time_buckets[bucket] += 1
        return {
            "total": total,
            "last_7_days": last_7_days,
            "missed_training": missed_training,
            "top_triggers": [{"name": r[0], "count": r[1]} for r in trigger_rows],
            "categories": [{"name": r[0], "count": r[1]} for r in category_rows],
            "severity_counts": [{"name": r[0], "count": r[1]} for r in severity_rows],
            "impact_counts": [{"name": r[0], "count": r[1]} for r in impact_rows],
            "time_buckets": time_buckets,
        }
    except Exception:
        return {
            "total": 0,
            "last_7_days": 0,
            "missed_training": 0,
            "top_triggers": [],
            "categories": [],
            "severity_counts": [],
            "impact_counts": [],
            "time_buckets": {"morning": 0, "afternoon": 0, "evening": 0},
        }


def get_all_problems():
    """All technique problems from DB + active flag."""
    problems = []
    if state.get("flag_for_bruno"):
        problems.append({"id": "active", "name": state["flag_for_bruno"], "tier": "urgent", "description": "Flagged for next Bruno session.", "resolved": False})
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT id, name, tier, description, resolved FROM problems ORDER BY created_at DESC LIMIT 20'
            ).fetchall()
        seen = {p["name"].lower() for p in problems}
        for r in rows:
            if r[1].lower() not in seen:
                problems.append({"id": r[0], "name": r[1], "tier": r[2], "description": r[3] or "", "resolved": bool(r[4])})
                seen.add(r[1].lower())
    except Exception:
        pass
    return problems


def get_streak_count():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='streak_count'").fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0

def get_streak_date():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='streak_date'").fetchone()
            return row[0] if row else None
    except Exception:
        return None

def set_streak_count(n):
    today = datetime.now(TZ).strftime('%Y-%m-%d')
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('streak_count', ?)", (str(n),))
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('streak_date', ?)", (today,))
            conn.commit()
    except Exception as e:
        logging.error(f"Streak save error: {e}")


def get_streak_days():
    """Consecutive days hitting WATER_GOAL_L, ending today (or yesterday if today not yet hit)."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT date, liters FROM daily_water ORDER BY date DESC LIMIT 60'
            ).fetchall()
    except Exception:
        return 0
    if not rows:
        history = {}
    else:
        history = {r[0]: (r[1] or 0) for r in rows}
    today = datetime.now(TZ).date()
    today_str = today.strftime('%Y-%m-%d')
    # Overlay in-memory total in case it hasn't been flushed to DB yet
    live_today = state.get("water_today", 0.0)
    if live_today > history.get(today_str, 0.0):
        history[today_str] = live_today
    cursor = today if history.get(today_str, 0) >= WATER_GOAL_L else (today - timedelta(days=1))
    streak = 0
    while True:
        d = cursor.strftime('%Y-%m-%d')
        if history.get(d, 0) >= WATER_GOAL_L:
            streak += 1
            cursor -= timedelta(days=1)
        else:
            break
    return streak


def get_today_schedule():
    """Return today's real training sessions for the dashboard Schedule page."""
    now = datetime.now(TZ)
    weekday = now.strftime('%a').lower()
    events = []
    if weekday in ('mon', 'wed', 'fri'):
        events.append({"time": "07:00", "label": "Drilling",          "kind": "training"})
        events.append({"time": "10:00", "label": "S&C with Roy",      "kind": "training"})
        events.append({"time": "14:00", "label": "Bruno private",     "kind": "training", "highlight": True})
    if weekday == 'mon':
        events.append({"time": "19:45", "label": "Evening class",     "kind": "training", "highlight": True})
    if weekday in ('tue', 'thu'):
        events.append({"time": "11:00", "label": "Stretch Zone",      "kind": "wellness"})
        events.append({"time": "14:00", "label": "Bruno private",     "kind": "training", "highlight": True})
        events.append({"time": "19:45", "label": "Competition class", "kind": "training", "highlight": True})
    if weekday == 'sun':
        events.append({"time": "20:30", "label": "Open mat",          "kind": "training", "highlight": True})
    events.sort(key=lambda e: e["time"])
    return events


def get_next_up():
    """Compute the next training session today based on weekday + current time."""
    now = datetime.now(TZ)
    weekday = now.strftime('%a').lower()
    events = []
    if weekday in ('mon', 'wed', 'fri'):
        events.append((7, 0, "Drilling · 7 or 8 AM"))
        events.append((10, 0, "S&C with Roy · 10 AM"))
        events.append((14, 0, "Bruno private · 2 PM"))
    if weekday == 'mon':
        events.append((19, 45, "Evening class · 7:45 PM"))
    if weekday in ('tue', 'thu'):
        events.append((11, 0, "Stretch Zone · 11 AM"))
        events.append((14, 0, "Bruno private · 2 PM"))
        events.append((19, 45, "Competition class · 7:45 PM"))
    # Dedupe (Bruno private can appear twice on Tue/Thu)
    seen = set()
    deduped = []
    for h, m, label in sorted(events, key=lambda e: e[0] * 60 + e[1]):
        if label in seen:
            continue
        seen.add(label)
        deduped.append((h, m, label))
    nm = now.hour * 60 + now.minute
    for h, m, label in deduped:
        if h * 60 + m >= nm:
            return label
    return None  # nothing left today


_bruno_summary_cache = {}  # {journal_id: summary} — avoid re-summarizing on every API hit
_bruno_summary_lock = threading.Lock()


def summarize_bruno(notes):
    """Run Claude to compress a private-lesson debrief into a headline (~10 words)."""
    if not claude or not notes:
        return (notes[:80] + "…") if notes and len(notes) > 80 else (notes or "")
    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=50,
            system=(
                "You compress BJJ private-lesson notes into a single short headline "
                "(8-12 words max). Capture the key technique, breakthrough, or struggle. "
                "Use Corey's voice — casual, present-tense, training-room language. "
                "Examples:\n"
                "- 'Collar drag re-grip clicked — landed it 3× in rolls.'\n"
                "- 'Worked passing closed guard — posture finally held.'\n"
                "- 'Spider lasso defense — still getting flattened.'\n"
                "Return ONLY the headline. No quotes, no preamble."
            ),
            messages=[{"role": "user", "content": notes}],
        )
        return response.content[0].text.strip().strip('"').strip("'")
    except Exception as e:
        logging.error(f"summarize_bruno error: {e}")
        return (notes[:80] + "…") if notes and len(notes) > 80 else (notes or "")


def get_bruno_lessons(limit=20):
    """All Bruno/Private journal entries with cached summaries."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT id, date, session, notes, created_at FROM journal "
                "WHERE session LIKE '%Bruno%' OR session LIKE '%Private%' "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except Exception:
        return []
    lessons = []
    for jid, date, session, notes, created_at in rows:
        summary = (notes[:80] + "…") if notes and len(notes) > 80 else (notes or "Bruno lesson")
        lessons.append({
            "id": f"L{jid}",
            "date": date,
            "session": session,
            "summary": summary,
            "notes": notes,
            "created_at": created_at,
        })
    return lessons


def get_bruno_recent():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT id, date, session, notes FROM journal "
                "WHERE session LIKE '%Bruno%' OR session LIKE '%Private%' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            jid, date, session, notes = row
            with _bruno_summary_lock:
                if jid not in _bruno_summary_cache:
                    _bruno_summary_cache[jid] = summarize_bruno(notes)
                summary = _bruno_summary_cache[jid]
            return {
                "date": date,
                "session": session,
                "notes": notes,
                "summary": summary,
            }
    except Exception:
        return None

def get_water_log_today():
    """Per-sip water entries for today."""
    today = datetime.now(TZ).strftime('%Y-%m-%d')
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT id, time, amount_l FROM water_log WHERE date = ? ORDER BY created_at ASC', (today,)
            ).fetchall()
        return [{"id": r[0], "time": r[1], "amount_ml": round(r[2] * 1000)} for r in rows]
    except Exception:
        return []


def get_water_history(days=6):
    """Last N days of water intake for the dashboard chart."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT date, liters FROM daily_water ORDER BY date DESC LIMIT ?', (days,)
            ).fetchall()
        return [{"date": r[0], "liters": r[1] or 0} for r in reversed(rows)]
    except Exception:
        return []


def get_recent_journal(n=15):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            return conn.execute(
                'SELECT date, session, notes FROM journal ORDER BY created_at DESC LIMIT ?', (n,)
            ).fetchall()
    except Exception:
        return []

# ── SYSTEM PROMPT (dynamic — injects journal) ─────────────────────────────────

def build_system_prompt():
    journal = get_recent_journal(15)

    if journal:
        journal_section = "\n\nCorey's training journal (most recent first — these are the ONLY facts you know about his past training, do not invent anything else):\n"
        for date, session, notes in journal:
            journal_section += f"- {date} [{session}]: {notes}\n"
        history_rule = "- Reference his training history naturally when it fits — but ONLY facts from the journal notes below. Never invent anything."
    else:
        journal_section = ""
        history_rule = "- You have no record of his past training yet. Do not make anything up. Just respond to what he says now."

    now = datetime.now(TZ).strftime("%A, %B %d %Y — %I:%M %p ET")
    check_and_reset_water()
    water_had = state["water_today"]
    water_left = round(WATER_GOAL_L - water_had, 2)
    if water_left <= 0:
        water_status = f"Water: hit his 3L goal today ✅"
    else:
        water_status = f"Water: only {water_had}L so far today — {water_left}L still needed. CRITICAL: Corey has been hospitalized 4 times for dehydration. Push water hard in every single message. Don't let it slide."

    return f"""You are Corey's training partner and close friend texting him on WhatsApp. You train BJJ too so you get it, but you're not his coach — don't give technique advice.

Current time: {now}
{water_status}

Rules:
- SHORT. 1-3 sentences max. This is texting.
- Casual and real. Talk like a friend, not a trainer. Use natural slang.
- When he tells you what he worked on, just acknowledge it and maybe ask one simple follow-up — not a technique deep dive, just genuine curiosity like a friend would.
- When he mentions struggling with something, just note it. Don't coach him. That's Bruno's job.
- When he mentions a tournament, hype him up and reference what he's been working on from his journal — but keep it simple and encouraging, not analytical.
- NEVER make up or invent training history. Only reference what's actually in the journal below or what Corey has said in this conversation.
{history_rule}
- NEVER pretend to know something you don't — places, people, things he mentions. If you don't know, just say "I don't know what that is, what is it?" Don't guess and don't fake it.
- IMPORTANT: This bot CAN see and log water photos automatically. If Corey sends a pic of a water bottle/glass, you analyze it and add to his daily total. So if he asks "can you see images?" — yes, you can, specifically for tracking water intake.
- If asked what model you are, you are Claude Sonnet 4.6 ({CLAUDE_MODEL}). Don't guess or make up old model versions.
- Emojis are fine but don't overdo it.

Corey's schedule:
- Mon/Wed/Fri: Drilling (7-8 or 8-9 AM), S&C with Roy (10-11 AM), Private with Bruno Malfacine (2-4 PM)
- Mon/Tue/Thu: Evening class with Bruno (7:45-9 PM)
- Tue/Thu: Stretch Zone (11-12 or 12-1 PM), Private with Bruno (2-4 PM), Competition Class (7:45-9 PM)
- Daily water goal: 3 liters{journal_section}"""

def ask_claude(user_msg):
    if not claude:
        return "Bot brain offline — API key missing"
    timestamp = datetime.now(TZ).strftime("%I:%M %p")
    timestamped_msg = f"[{timestamp}] {user_msg}"
    chat_history.append({"role": "user", "content": timestamped_msg})
    save_message("user", timestamped_msg)
    # Keep last 20 messages for context
    if len(chat_history) > 20:
        chat_history.pop(0)
    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=300,
            system=build_system_prompt(),
            messages=chat_history,
        )
    except Exception as e:
        chat_history.pop()  # roll back user message on failure
        logging.error(f"Claude API error: {e}")
        return "Yo my bad, brain glitched for a sec. Say that again?"
    reply = response.content[0].text
    chat_history.append({"role": "assistant", "content": reply})
    save_message("assistant", reply)
    return reply

def claude_is_skip(user_msg, question_context):
    """Ask Claude to judge if Corey is skipping the training session. Returns True/False."""
    if not claude:
        return False
    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=10,
            system="You are a classifier. Reply with ONLY 'YES' or 'NO', nothing else.",
            messages=[{
                "role": "user",
                "content": f"Corey was asked: \"{question_context}\"\nCorey replied: \"{user_msg}\"\n\nIs Corey saying he is NOT going to train / is skipping / not attending? Reply YES or NO only."
            }]
        )
        answer = response.content[0].text.strip().upper()
        return answer.startswith("YES")
    except Exception as e:
        logging.error(f"Claude skip-check error: {e}")
        return False

# Conversation state (in-memory, resets on redeploy — fine for now)
state = {
    "last_question": None,       # what we're waiting on
    "last_question_time": None,  # datetime when last_question was set (H8)
    "drilling_time": None,       # 7 or 8
    "stretch_time": None,        # 11 or 12
    "awaiting_reply": False,     # follow-up tracking
    "partner_pending": None,     # waiting on partner reply
    "replying_to": None,         # which partner Corey is responding to
    "followup_index": 0,         # how many follow-ups have fired
    "followup_delays": [],       # cadence for current follow-up chain
    "water_today": 0.0,          # liters consumed today
    "water_date": None,          # "YYYY-MM-DD" — resets daily
    "debrief_session": None,     # session type being debriefed after class
    "debrief_time": None,        # datetime when debrief_session was set (H9)
    "debrief_step": None,        # current step in the debrief interview
    "debrief_headline": None,    # e.g. "De La Riva passing"
    "debrief_one_liner": None,   # e.g. "Couldn't stack when he kept hips heavy"
    "_problem_position": None,
    "_problem_issue": None,
    "_technique_check_pending": None,  # technique awaiting folder-check confirmation
    "_video_technique_log_id": None,  # technique_log row id waiting for video
    "_injury_body_part": None,
    "_injury_severity": None,
    "_injury_description": None,
    "_injury_partner": None,
    "_injury_when": None,
    # Allergy intake (NEW — standalone interview when user reports allergy symptoms)
    "_allergy_trigger": None,
    "_allergy_symptoms": None,
    "_allergy_severity": None,
    "_allergy_medication": None,
    "_allergy_training_impact": None,
    "_allergy_missed_training": None,
    # Standalone intake mode: when user spontaneously reports something
    # without being mid-debrief. Set to a label like "injury", "problem",
    # "allergy" so the existing debrief handlers can short-circuit cleanup
    # back to general chat instead of flowing into the next debrief step.
    "intake_mode": None,
    "flag_for_bruno": None,      # technique to flag in the next private reminder
    "rest_day": False,           # if True, suppress all session reminders for today
    "rest_day_date": None,       # date the rest day was set
}

# Schedule follow-ups: every 15 min, 3 times
FOLLOWUP_DELAYS_MIN  = [15, 30, 45]
# Water follow-ups: every 5 min, 2 times
WATER_FOLLOWUP_DELAYS = [5, 10]


# ── WATER TRACKING ────────────────────────────────────────────────────────────

def is_rest_day():
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if state["rest_day"] and state["rest_day_date"] == today:
        return True
    state["rest_day"] = False  # auto-reset on new day
    return False

def set_rest_day():
    state["rest_day"] = True
    state["rest_day_date"] = datetime.now(TZ).strftime("%Y-%m-%d")

def check_and_reset_water():
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if state["water_date"] != today:
        if state["water_today"] < WATER_GOAL_L:
            set_streak_count(0)
        state["water_today"] = 0.0
        state["water_date"] = today

def maybe_increment_streak(liters_just_added):
    """Increment streak if this addition just crossed the daily goal for the first time today."""
    today_str = datetime.now(TZ).strftime('%Y-%m-%d')
    previously_under = (state["water_today"] - liters_just_added) < WATER_GOAL_L
    just_hit = state["water_today"] >= WATER_GOAL_L
    if previously_under and just_hit and get_streak_date() != today_str:
        set_streak_count(get_streak_count() + 1)


def estimate_water_from_image(image_bytes, content_type):
    if not claude:
        return None
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": content_type, "data": b64}
                    },
                    {
                        "type": "text",
                        "text": (
                            "Look at this photo of a water container (bottle, glass, cup, etc.).\n"
                            "Estimate how many liters of water were consumed based on what you see.\n"
                            "- For branded bottles: identify brand and size, estimate fraction consumed.\n"
                            "- For restaurant glasses or generic cups: estimate the volume (a standard restaurant glass is ~0.35-0.45L, a large glass is ~0.5L).\n"
                            "- If multiple glasses/containers are visible, add them up.\n"
                            "Examples: full Fiji 1L = 1.0, half Dasani 500mL = 0.25, two restaurant glasses = 0.8.\n"
                            "Reply with ONLY a decimal number in liters. Nothing else."
                        )
                    }
                ]
            }]
        )
        text = response.content[0].text.strip()
        logging.info(f"Vision raw response: {text}")
        match = re.search(r'\d+\.?\d*', text)
        return float(match.group()) if match else None
    except Exception as e:
        logging.error(f"Water vision error: {e}")
        return None

# ── SEND ──────────────────────────────────────────────────────────────────────

FOLLOWUPS = [
    "You good? I ain't hear back from you",
    "Hello?? Don't leave me on read lol",
    "Bro I know you saw that 😂 answer me",
]

def send_to(number, msg):
    if not ACCOUNT_SID or not AUTH_TOKEN:
        logging.warning(f"Twilio credentials missing; not sending to {number}: {msg}")
        return {"ok": False, "error": "twilio_credentials_missing"}
    try:
        message = client.messages.create(body=msg, from_=FROM_NUMBER, to=number)
        logging.info(f"SENT to {number}: {msg}")
        return {"ok": True, "sid": getattr(message, "sid", None)}
    except Exception as e:
        logging.error(f"send_to error ({number}): {e}")
        return {"ok": False, "error": "twilio_send_failed", "detail": str(e)}

def send(msg, followup=False, delays=None):
    if not ACCOUNT_SID or not AUTH_TOKEN:
        logging.warning(f"Twilio credentials missing; not sending: {msg}")
        return {"ok": False, "error": "twilio_credentials_missing"}
    try:
        message = client.messages.create(body=msg, from_=FROM_NUMBER, to=MY_NUMBER)
        logging.info(f"SENT: {msg}")
        result = {"ok": True, "sid": getattr(message, "sid", None)}
    except Exception as e:
        logging.error(f"send error: {e}")
        return {"ok": False, "error": "twilio_send_failed", "detail": str(e)}
    if followup:
        state["awaiting_reply"] = True
        state["followup_index"] = 0
        state["followup_delays"] = delays or FOLLOWUP_DELAYS_MIN
        run_at = datetime.now(TZ) + timedelta(minutes=state["followup_delays"][0])
        scheduler.add_job(send_followup, 'date', run_date=run_at,
                          id='followup', replace_existing=True)
    return result

def water_progress():
    check_and_reset_water()
    had = state["water_today"]
    left = round(WATER_GOAL_L - had, 2)
    if left <= 0:
        return f"You already hit your 3L today 💧🎉"
    return f"You've had {had}L today — {left}L to go 💧"

def send_water(msg):
    send(f"{msg} {water_progress()}", followup=True, delays=WATER_FOLLOWUP_DELAYS)

def send_followup():
    if not state.get("awaiting_reply"):
        return
    idx = state["followup_index"]
    if idx >= len(FOLLOWUPS):
        state["awaiting_reply"] = False
        return
    send(FOLLOWUPS[idx], followup=False)
    state["followup_index"] = idx + 1
    next_idx = state["followup_index"]
    delays = state.get("followup_delays", FOLLOWUP_DELAYS_MIN)
    if next_idx < len(delays):
        run_at = datetime.now(TZ) + timedelta(minutes=delays[next_idx])
        scheduler.add_job(send_followup, 'date', run_date=run_at,
                          id='followup', replace_existing=True)
    else:
        state["awaiting_reply"] = False

def _normalize_technique(name):
    """Normalize technique name: lowercase, strip, collapse whitespace. No semantic merging."""
    return " ".join(name.lower().strip().split())


def get_or_create_technique(name):
    """Find or create a technique folder. Returns (id, is_new)."""
    normalized = _normalize_technique(name)
    if not normalized:
        return None, True
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute('SELECT id FROM techniques WHERE name = ?', (normalized,)).fetchone()
            if row:
                return row[0], False
            cursor = conn.execute(
                'INSERT INTO techniques (name, created_at) VALUES (?,?)',
                (normalized, datetime.now(TZ).isoformat())
            )
            conn.commit()
            return cursor.lastrowid, True
    except Exception as e:
        logging.error(f"get_or_create_technique error: {e}")
        return None, True


def log_technique_note(technique_id, session, notes, sentiment):
    """Append a session note to a technique folder. Returns inserted row id."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.execute(
                'INSERT INTO technique_log (technique_id, date, session, notes, sentiment, created_at) VALUES (?,?,?,?,?,?)',
                (technique_id, datetime.now(TZ).strftime('%Y-%m-%d'), session, notes, sentiment, datetime.now(TZ).isoformat())
            )
            conn.commit()
            return cursor.lastrowid
    except Exception as e:
        logging.error(f"log_technique_note error: {e}")
        return None


def update_technique_log_video(log_id, video_url):
    """Attach a video URL to a technique_log entry."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('UPDATE technique_log SET video_url=? WHERE id=?', (video_url, log_id))
            conn.commit()
    except Exception as e:
        logging.error(f"update_technique_log_video error: {e}")


def upload_to_cloudinary(media_url):
    """Download media from Twilio URL and upload to Cloudinary. Returns secure_url or None."""
    if not CLOUDINARY_CLOUD_NAME or not CLOUDINARY_API_KEY or not CLOUDINARY_API_SECRET:
        logging.warning("Cloudinary credentials not configured")
        return None
    try:
        # Download from Twilio (needs auth)
        media_response = req.get(
            media_url,
            auth=(ACCOUNT_SID, AUTH_TOKEN),
            timeout=30,
        )
        if not media_response.ok:
            logging.error(f"Failed to download media: {media_response.status_code}")
            return None
        media_bytes = media_response.content
        content_type = media_response.headers.get('Content-Type', 'video/mp4')
        # Upload to Cloudinary via REST API
        import hashlib, time as _time
        timestamp = str(int(_time.time()))
        folder = "bjj-technique-videos"
        params_to_sign = f"folder={folder}&timestamp={timestamp}"
        signature = hashlib.sha1(
            f"{params_to_sign}{CLOUDINARY_API_SECRET}".encode()
        ).hexdigest()
        upload_url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/video/upload"
        files = {"file": (f"technique_{timestamp}.mp4", media_bytes, content_type)}
        data = {
            "api_key": CLOUDINARY_API_KEY,
            "timestamp": timestamp,
            "signature": signature,
            "folder": folder,
        }
        upload_resp = req.post(upload_url, files=files, data=data, timeout=120)
        if upload_resp.ok:
            result = upload_resp.json()
            url = result.get("secure_url")
            logging.info(f"Cloudinary upload success: {url}")
            return url
        else:
            logging.error(f"Cloudinary upload failed: {upload_resp.text[:200]}")
            return None
    except Exception as e:
        logging.error(f"upload_to_cloudinary error: {e}")
        return None


def get_technique_history(technique_id, limit=5):
    """Get past notes for a technique folder, newest first, excluding today."""
    today = datetime.now(TZ).strftime('%Y-%m-%d')
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT date, session, notes, sentiment, video_url FROM technique_log '
                'WHERE technique_id = ? AND date < ? ORDER BY created_at DESC LIMIT ?',
                (technique_id, today, limit)
            ).fetchall()
        return [{"date": r[0], "session": r[1], "notes": r[2], "sentiment": r[3], "video_url": r[4]} for r in rows]
    except Exception:
        return []


def extract_techniques(notes):
    """Extract all BJJ techniques from notes. Returns [{name, sentiment}] or []."""
    if not claude or not notes:
        return []
    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=250,
            system=(
                "Extract BJJ technique names from training notes. "
                "Return a JSON array of objects: [{\"name\": \"spider lasso\", \"sentiment\": \"struggled\"}]. "
                "sentiment must be one of: \"learned\" (figured it out/clicked), \"struggled\" (had trouble/issue), \"worked_on\" (neutral). "
                "Keep technique names exactly as stated — never merge similar techniques. "
                "Only named techniques, not vague descriptions. Return [] if none. Return ONLY valid JSON, no markdown."
            ),
            messages=[{"role": "user", "content": notes}],
        )
        raw = re.sub(r'^```\w*\s*|\s*```$', '', response.content[0].text.strip()).strip()
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception as e:
        logging.error(f"extract_techniques error: {e}")
        return []


def generate_technique_summary(technique_id, name):
    """Generate a short summary of all notes for a technique folder."""
    if not claude:
        return None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT date, notes FROM technique_log WHERE technique_id = ? ORDER BY created_at ASC',
                (technique_id,)
            ).fetchall()
        if not rows:
            return None
        combined = "\n\n".join(f"[{r[0]}]: {r[1]}" for r in rows)
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=150,
            system=(
                "You write short, punchy summaries of a BJJ athlete's progress on a specific technique. "
                "2-3 sentences max. Capture the arc — what they struggled with, what clicked, where they are now. "
                "Use the athlete's own language and voice. No generic advice."
            ),
            messages=[{"role": "user", "content": f"Technique: {name}\n\nSession notes:\n{combined}"}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        logging.error(f"generate_technique_summary error: {e}")
        return None


def get_all_techniques():
    """All technique folders with note count, last seen date, and video count."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT t.id, t.name, t.summary, COUNT(l.id) as cnt, MAX(l.date) as last_seen, '
                'SUM(CASE WHEN l.video_url IS NOT NULL THEN 1 ELSE 0 END) as video_count '
                'FROM techniques t LEFT JOIN technique_log l ON t.id = l.technique_id '
                'GROUP BY t.id ORDER BY last_seen DESC LIMIT 100'
            ).fetchall()
        return [{"id": r[0], "name": r[1], "summary": r[2], "count": r[3], "last_seen": r[4], "video_count": r[5] or 0} for r in rows]
    except Exception:
        return []


# ── SCHEDULED MESSAGES ────────────────────────────────────────────────────────

def ask_drilling_time():
    state["drilling_time"] = None
    state["last_question"] = "drilling_time"
    state["last_question_time"] = datetime.now(TZ)
    send("Yo what time you drilling this morning — 7 or 8?", followup=True)

def ask_stretch_time():
    state["stretch_time"] = None
    state["last_question"] = "stretch_time"
    state["last_question_time"] = datetime.now(TZ)
    send("Stretch Zone today — 11 or 12?", followup=True)

def checkin_after_drilling():
    state["last_question"] = None
    state["drilling_time"] = None
    state["debrief_session"] = "Drilling"
    state["debrief_time"] = datetime.now(TZ)
    state["debrief_step"] = "headline"
    state["debrief_headline"] = None
    state["debrief_one_liner"] = None
    send("yo how was drilling, what were you guys working on?")

def remind_sc():
    if is_rest_day(): return
    send("S&C with Roy in 15 — you ready to suffer lol")
    send_water("You got your water for S&C? 💧")

def checkin_after_sc():
    if is_rest_day(): return
    send("You make it through Roy today? 💀")

def remind_private():
    if is_rest_day(): return
    flag = state.get("flag_for_bruno")
    if flag:
        send(f"Bruno private in 15 — you mentioned having trouble with {flag} earlier, good time to bring that up 🥋")
        state["flag_for_bruno"] = None
    else:
        send("Bruno private in 15 — get your head right 🥋")
    send_water("Water before the private 💧")

def checkin_after_private():
    if is_rest_day(): return
    state["debrief_session"] = "Private with Bruno"
    state["debrief_time"] = datetime.now(TZ)
    state["debrief_step"] = "headline"
    state["debrief_headline"] = None
    state["debrief_one_liner"] = None
    send("yo how was the private, what did you and Bruno work on today?")

def remind_stretch():
    if is_rest_day(): return
    send("Stretch Zone coming up — you heading out?")
    send_water("Bring that water to Stretch Zone 💧")

def checkin_after_stretch():
    if is_rest_day(): return
    state["last_question"] = None
    state["stretch_time"] = None
    send("Body feeling better after Stretch Zone?")

def remind_evening():
    if is_rest_day(): return
    send("Evening class with Bruno at 7:45 — you on your way?")
    send_water("Sip that water before you head out 💧")

def checkin_after_evening():
    if is_rest_day(): return
    state["debrief_session"] = "Evening class"
    state["debrief_time"] = datetime.now(TZ)
    state["debrief_step"] = "headline"
    state["debrief_headline"] = None
    state["debrief_one_liner"] = None
    send("yo how was class tonight, what were you working on?")

# ── PARTNER MESSAGES ──────────────────────────────────────────────────────────

def ask_partner_drilling():
    for name, number in PARTNERS.items():
        send_to(number, f"Hey what's up {name}! How you doing big man? Just wanted to confirm for drilling tomorrow — is it from 7 to 8 or 8 to 9?")
        state["partner_pending"] = name
    logging.info("Asked partners about drilling")

def water_penalty_check():
    check_and_reset_water()
    if state["water_today"] >= WATER_GOAL_L:
        return
    had = state["water_today"]
    left = round(WATER_GOAL_L - had, 2)
    send(
        f"Only {had}L today — champions don't miss the small stuff bro 💧 "
        f"{left}L short means 15 push-ups, 15 squats, 5 burpees before you sleep. "
        f"Every little thing adds up. You know that."
    )

def water_late_night():
    send_water("Yo it's late — still drinking?")

def water_morning():
    send_water("Start sipping early —")

def water_afternoon():
    send_water("Mid-day check —")

def water_evening():
    send_water("Almost end of day —")

# ── WEBHOOK (incoming replies from you) ───────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook():
    raw_body = request.form.get('Body', '').strip()
    body = raw_body.lower()
    sender = request.form.get('From', '')
    resp = MessagingResponse()

    # Handle photo from Corey — water intake tracking
    if int(request.form.get('NumMedia', 0)) > 0 and sender == MY_NUMBER:
        media_url     = request.form.get('MediaUrl0')
        content_type  = request.form.get('MediaContentType0', 'image/jpeg')
        image_resp    = req.get(media_url, auth=(ACCOUNT_SID, AUTH_TOKEN))
        liters        = estimate_water_from_image(image_resp.content, content_type)
        if liters is None:
            resp.message("Couldn't read that pic — just text me liters (e.g. '1' or '0.5')")
        elif liters == 0:
            resp.message("That glass is empty bro 💀 fill it up and send another pic 💧")
        else:
            check_and_reset_water()
            state["water_today"] = round(state["water_today"] + liters, 2)
            save_water_to_db()
            maybe_increment_streak(liters)
            try:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        'INSERT INTO water_log (date, time, amount_l, created_at) VALUES (?,?,?,?)',
                        (datetime.now(TZ).strftime('%Y-%m-%d'), datetime.now(TZ).strftime('%H:%M'), liters, datetime.now(TZ).isoformat())
                    )
                    conn.commit()
            except Exception as e:
                logging.error(f"water_log insert error: {e}")
            remaining = round(WATER_GOAL_L - state["water_today"], 2)
            if remaining <= 0:
                resp.message(f"LET'S GO!! You hit your {WATER_GOAL_L}L goal today 🎉💧")
            else:
                resp.message(f"+{liters}L logged 💧 You've had {state['water_today']}L today — {remaining}L to go")
        return str(resp)

    # Check if message is from a partner — relay to Corey
    partner_name = None
    for name, number in PARTNERS.items():
        if sender == number:
            partner_name = name
            break

    if partner_name:
        # Forward partner's reply to Corey — nag him to respond
        state["awaiting_reply"] = False
        state["followup_index"] = 0
        try:
            scheduler.remove_job('followup')
        except Exception:
            pass
        send(f"{partner_name} said: \"{raw_body}\"\n\nWhat do you want me to reply?", followup=True)
        state["last_question"] = "partner_reply"
        state["replying_to"] = partner_name
        return str(resp)

    # It's from Corey — cancel any pending follow-up
    state["awaiting_reply"] = False
    state["followup_index"] = 0
    try:
        scheduler.remove_job('followup')
    except Exception:
        pass

    last_q = state.get("last_question")

    # If Corey is replying to a partner message
    if last_q == "partner_reply":
        partner = state.get("replying_to")
        if partner and partner in PARTNERS:
            send_to(PARTNERS[partner], raw_body)
            resp.message(f"Sent to {partner} 👊")
        state["last_question"] = None
        state["replying_to"] = None
        return str(resp)

    if last_q == "drilling_time":
        _lqt = state.get("last_question_time")
        if _lqt and (datetime.now(TZ) - _lqt).total_seconds() > 3600:
            state["last_question"] = None
            state["last_question_time"] = None
            resp.message(ask_claude(raw_body))
            return str(resp)
        if "7" in body:
            state["drilling_time"] = 7
            state["last_question"] = None
            resp.message("Bet — drilling at 7. I'll check in after 🔥")
            send_water("Start sipping now —")
            run_at = datetime.now(TZ).replace(hour=8, minute=15, second=0, microsecond=0)
            if datetime.now(TZ) < run_at:
                scheduler.add_job(checkin_after_drilling, 'date', run_date=run_at,
                                  id='drill_checkin', replace_existing=True)
        elif "8" in body:
            state["drilling_time"] = 8
            state["last_question"] = None
            resp.message("Bet — drilling at 8. Got you 👊")
            send_water("Start sipping now —")
            run_at = datetime.now(TZ).replace(hour=9, minute=15, second=0, microsecond=0)
            if datetime.now(TZ) < run_at:
                scheduler.add_job(checkin_after_drilling, 'date', run_date=run_at,
                                  id='drill_checkin', replace_existing=True)
        elif claude_is_skip(raw_body, "Yo what time you drilling this morning — 7 or 8?"):
            state["last_question"] = None
            state["drilling_time"] = None
            set_rest_day()
            resp.message(ask_claude(raw_body))
        else:
            state["last_question"] = None
            state["drilling_time"] = None
            resp.message(ask_claude(raw_body))

    elif last_q == "stretch_time":
        _lqt = state.get("last_question_time")
        if _lqt and (datetime.now(TZ) - _lqt).total_seconds() > 3600:
            state["last_question"] = None
            state["last_question_time"] = None
            resp.message(ask_claude(raw_body))
            return str(resp)
        if "11" in body:
            state["stretch_time"] = 11
            state["last_question"] = None
            resp.message("Got it — Stretch Zone at 11 🙆")
            send_water("Drink some water before you get there 💧")
            remind_at  = datetime.now(TZ).replace(hour=10, minute=45, second=0, microsecond=0)
            checkin_at = datetime.now(TZ).replace(hour=12, minute=5,  second=0, microsecond=0)
            if datetime.now(TZ) < remind_at:
                scheduler.add_job(remind_stretch, 'date', run_date=remind_at,
                                  id='stretch_remind', replace_existing=True)
            if datetime.now(TZ) < checkin_at:
                scheduler.add_job(checkin_after_stretch, 'date', run_date=checkin_at,
                                  id='stretch_checkin', replace_existing=True)
        elif "12" in body:
            state["stretch_time"] = 12
            state["last_question"] = None
            resp.message("Got it — Stretch Zone at 12 🙆")
            send_water("Drink some water before you get there 💧")
            remind_at  = datetime.now(TZ).replace(hour=11, minute=45, second=0, microsecond=0)
            checkin_at = datetime.now(TZ).replace(hour=13, minute=5,  second=0, microsecond=0)
            if datetime.now(TZ) < remind_at:
                scheduler.add_job(remind_stretch, 'date', run_date=remind_at,
                                  id='stretch_remind', replace_existing=True)
            if datetime.now(TZ) < checkin_at:
                scheduler.add_job(checkin_after_stretch, 'date', run_date=checkin_at,
                                  id='stretch_checkin', replace_existing=True)
        elif claude_is_skip(raw_body, "Stretch Zone today — 11 or 12?"):
            state["last_question"] = None
            state["stretch_time"] = None
            resp.message(ask_claude(raw_body))
        else:
            state["last_question"] = None
            state["stretch_time"] = None
            resp.message(ask_claude(raw_body))

    else:
        # Multi-step debrief interview
        if (state.get("debrief_session") or state.get("intake_mode")) and state.get("debrief_step"):
            _dbt = state.get("debrief_time")
            if _dbt and (datetime.now(TZ) - _dbt).total_seconds() > 14400:
                state["debrief_session"] = None
                state["debrief_time"] = None
                state["debrief_step"] = None
                state["debrief_headline"] = None
                state["debrief_one_liner"] = None
                resp.message(ask_claude(raw_body))
                return str(resp)

            step = state["debrief_step"]

            if step == "headline":
                interp = interpret_debrief_reply("headline", raw_body)
                if interp.get("unclear"):
                    resp.message(interp.get("ask", "what technique did you work on?"))
                    return str(resp)
                if interp.get("skip"):
                    # Skip debrief entirely
                    state["debrief_session"] = None
                    state["debrief_step"] = None
                    state["debrief_time"] = None
                    resp.message("all good, no worries 👊")
                    return str(resp)
                headline = interp.get("value", raw_body).strip()
                state["debrief_headline"] = headline
                state["debrief_step"] = "full_notes"
                state["debrief_time"] = datetime.now(TZ)
                resp.message(f"nice, {headline} 👊 give me the full breakdown — what was the set-up, what clicked, what didn't, how many rounds? say as much or as little as you want")

            elif step == "full_notes":
                # Check if this is a correction to the headline
                interp = interpret_debrief_reply("full_notes", raw_body, {
                    "headline": state.get("debrief_headline", "")
                })
                if interp.get("correction") and interp.get("field") == "headline":
                    corrected = interp.get("value", raw_body).strip()
                    state["debrief_headline"] = corrected
                    state["debrief_time"] = datetime.now(TZ)
                    resp.message(f"got it, {corrected} 👊 now give me the full breakdown — what was the set-up, what clicked, what didn't?")
                    return str(resp)
                if interp.get("unclear"):
                    resp.message(interp.get("ask", "tell me what happened in the session"))
                    return str(resp)
                # Use the interpreted value or raw_body
                headline = state.get("debrief_headline") or ""
                full_notes = interp.get("value", raw_body).strip() if interp.get("understood") else raw_body.strip()
                notes = f"{headline}\n\n{full_notes}".strip()
                session_type = state["debrief_session"]
                save_journal_entry(session_type, notes)
                issue = extract_issue(notes)
                if issue:
                    state["flag_for_bruno"] = issue

                # Extract techniques and log them
                techniques = extract_techniques(notes)
                folder_to_check = None
                last_log_id = None

                for tech in techniques:
                    tech_name = (tech.get("name") or "").strip()
                    if not tech_name:
                        continue
                    tid, is_new = get_or_create_technique(tech_name)
                    if tid:
                        log_id = log_technique_note(tid, session_type, notes, tech.get("sentiment", "worked_on"))
                        if log_id:
                            last_log_id = log_id
                        if not is_new and tech.get("sentiment") == "struggled" and folder_to_check is None:
                            folder_to_check = (tech_name, tid)
                        # Regenerate technique summary
                        try:
                            new_summary = generate_technique_summary(tid, tech_name)
                            if new_summary:
                                with sqlite3.connect(DB_PATH) as conn:
                                    conn.execute('UPDATE techniques SET summary=? WHERE id=?', (new_summary, tid))
                                    conn.commit()
                        except Exception:
                            pass

                state["_video_technique_log_id"] = last_log_id
                if folder_to_check:
                    state["_technique_check_pending"] = folder_to_check[0]

                # New order: problem_check → video_check → injury_check
                state["debrief_step"] = "problem_check"
                state["debrief_time"] = datetime.now(TZ)
                resp.message("logged 🥋 anything you kept getting stuck on that you wanna flag for Bruno?")

            elif step == "technique_folder_check":
                tech_name = state.get("_technique_check_pending", "")
                lower = raw_body.lower().strip()
                yes_words = ["yes", "yeah", "yep", "yup", "sure", "check", "go ahead", "please", "ok", "okay", "y"]
                wants_check = any(w in lower for w in yes_words)

                if wants_check and tech_name:
                    tid, _ = get_or_create_technique(tech_name)
                    if tid:
                        history = get_technique_history(tid, limit=3)
                        if history:
                            lines = []
                            for h in history:
                                snippet = h["notes"][:250] + ("…" if len(h["notes"]) > 250 else "")
                                lines.append(f"📅 {h['date']} ({h['session']}):\n\"{snippet}\"")
                            resp.message(f"Here's what you had on {tech_name}:\n\n" + "\n\n".join(lines))
                        else:
                            resp.message(f"Folder exists for {tech_name} but no older notes found yet.")
                    else:
                        resp.message("Couldn't load that folder.")
                else:
                    resp.message("Got it, moving on.")

                # Always proceed to injury check
                state["debrief_step"] = "injury_check"
                state["debrief_time"] = datetime.now(TZ)
                state["_technique_check_pending"] = None
                return str(resp)

            elif step == "video_check":
                # Check if this is an MMS with video/image
                num_media = int(request.form.get('NumMedia', 0))
                if num_media > 0:
                    media_url = request.form.get('MediaUrl0', '')
                    media_type = request.form.get('MediaContentType0', '')
                    log_id = state.get("_video_technique_log_id")
                    if media_url and log_id and ('video' in media_type or 'image' in media_type):
                        resp.message("Got it, uploading your video 📹 This might take a sec...")
                        video_url = upload_to_cloudinary(media_url)
                        if video_url:
                            update_technique_log_video(log_id, video_url)
                            resp.message("Video saved to your technique folder ✅")
                        else:
                            resp.message("Couldn't upload the video, but everything else is saved.")
                    else:
                        resp.message("Couldn't process that media, moving on.")
                else:
                    # Text response — check for skip
                    lower = raw_body.lower().strip()
                    skip_words = ["skip", "no", "nah", "nope", "none", "n/a", "not now"]
                    if not any(w in lower for w in skip_words):
                        resp.message("Send the video as a WhatsApp message, or say 'skip' to move on.")
                        return str(resp)  # stay on this step
                # Move to technique_folder_check or injury_check
                state["_video_technique_log_id"] = None
                pending = state.get("_technique_check_pending")
                if pending:
                    state["debrief_step"] = "technique_folder_check"
                    resp.message(f"you've had notes on {pending} before — want me to pull the folder and show the recent history?")
                else:
                    state["debrief_step"] = "injury_check"
                    resp.message("any injuries, tweaks, or pain from today?")
                state["debrief_time"] = datetime.now(TZ)
                return str(resp)

            elif step == "injury_check":
                lower = raw_body.lower()
                clear_skip = ["no", "nah", "nope", "all good", "fine", "nothing", "n/a", "na", "good", "im good", "i'm good", "feels good", "feel good", "nothing wrong"]
                if any(w in lower for w in clear_skip):
                    state["debrief_session"] = None
                    state["debrief_step"] = None
                    state["debrief_headline"] = None
                    state["debrief_time"] = None
                    resp.message("all good, everything's logged 💪 keep grinding")
                else:
                    interp = interpret_debrief_reply("injury_check", raw_body)
                    if interp.get("skip"):
                        state["debrief_session"] = None
                        state["debrief_step"] = None
                        state["debrief_headline"] = None
                        state["debrief_time"] = None
                        resp.message("all good, everything's logged 💪 keep grinding")
                    else:
                        body_part = interp.get("value", raw_body).strip()
                        state["_injury_body_part"] = body_part
                        state["debrief_step"] = "injury_severity"
                        state["debrief_time"] = datetime.now(TZ)
                        resp.message(f"got it — {body_part}. how bad is it?\n\n• *Managing* — minor, barely noticeable\n• *Watch* — noticeable, keeping an eye on it\n• *Fresh* — happened recently, needs attention\n• *Critical* — serious, may need to stop training")

            elif step == "injury_severity":
                lower = raw_body.lower().strip()
                severity_map = {
                    "managing": "managing", "manage": "managing",
                    "watch": "watch", "watching": "watch",
                    "fresh": "fresh",
                    "critical": "critical", "serious": "critical",
                }
                severity = next((v for k, v in severity_map.items() if k in lower), None)
                if severity is None:
                    resp.message("just pick one: managing, watch, fresh, or critical")
                    return str(resp)
                state["_injury_severity"] = severity
                state["debrief_step"] = "injury_description"
                state["debrief_time"] = datetime.now(TZ)
                resp.message("What happened? Describe it — how it occurred, what makes it worse, anything relevant.")

            elif step == "injury_description":
                interp = interpret_debrief_reply("injury_description", raw_body, {
                    "body_part": state.get("_injury_body_part", "")
                })
                if interp.get("unclear"):
                    resp.message(interp.get("ask", "describe what happened — how did it occur, what makes it worse?"))
                    return str(resp)
                state["_injury_description"] = interp.get("value", raw_body).strip()
                state["debrief_step"] = "injury_partner"
                state["debrief_time"] = datetime.now(TZ)
                resp.message("who were you training with when this happened? (partner's name, or say 'solo' if alone)")

            elif step == "injury_partner":
                interp = interpret_debrief_reply("injury_partner", raw_body)
                if interp.get("skip") or "solo" in raw_body.lower() or "alone" in raw_body.lower():
                    state["_injury_partner"] = "solo"
                else:
                    state["_injury_partner"] = interp.get("value", raw_body).strip()
                state["debrief_step"] = "injury_when"
                state["debrief_time"] = datetime.now(TZ)
                resp.message("when did it happen? (e.g. 'today during drilling', 'earlier this week', 'last private')")

            elif step == "injury_when":
                interp = interpret_debrief_reply("injury_when", raw_body)
                state["_injury_when"] = interp.get("value", raw_body).strip()
                state["debrief_step"] = "injury_rest_plan"
                state["debrief_time"] = datetime.now(TZ)
                resp.message("what's your rest plan?\n\n• Resting it\n• Getting PT\n• Training through it\n• Not sure yet")

            elif step == "injury_rest_plan":
                interp = interpret_debrief_reply("injury_rest_plan", raw_body)
                rest_plan = interp.get("value", raw_body).strip()
                body_part = state.get("_injury_body_part", "")
                severity = state.get("_injury_severity", "watch")
                description = state.get("_injury_description", "")
                region = map_body_region(body_part)
                notes = f"Description: {description}\nRest plan: {rest_plan}\nRegion: {region}"
                try:
                    with sqlite3.connect(DB_PATH) as conn:
                        conn.execute(
                            'INSERT INTO injuries (date, body_part, severity, notes, partner, when_happened, created_at) VALUES (?,?,?,?,?,?,?)',
                            (datetime.now(TZ).strftime('%Y-%m-%d'), body_part, severity, notes,
                             state.get("_injury_partner"), state.get("_injury_when"), datetime.now(TZ).isoformat())
                        )
                        conn.commit()
                    logging.info(f"INJURY logged: {body_part} ({severity})")
                except Exception as e:
                    logging.error(f"Injury log error: {e}")
                state["_injury_body_part"] = None
                state["_injury_severity"] = None
                state["_injury_description"] = None
                state["_injury_partner"] = None
                state["_injury_when"] = None
                state["debrief_session"] = None
                state["debrief_step"] = None
                state["debrief_headline"] = None
                state["debrief_time"] = None
                state["intake_mode"] = None
                severity_emoji = {"managing": "🩹", "watch": "👀", "fresh": "🩼", "critical": "🚨"}.get(severity, "🩹")
                resp.message(f"logged {body_part} ({severity}) {severity_emoji} all good, everything's saved 💪")

            elif step == "problem_check":
                # Check for corrections to earlier steps
                interp = interpret_debrief_reply("problem_check", raw_body, {
                    "headline": state.get("debrief_headline", ""),
                })
                if interp.get("correction") and interp.get("field") == "headline":
                    corrected = interp.get("value", raw_body).strip()
                    state["debrief_headline"] = corrected
                    state["debrief_time"] = datetime.now(TZ)
                    resp.message(f"updated — technique is {corrected}. anything you kept getting stuck on that you wanna flag for Bruno?")
                    return str(resp)
                lower = raw_body.lower()
                skip_words = ["no", "nah", "nope", "all good", "nothing", "n/a", "na", "not really", "nope"]
                is_skip = any(w in lower for w in skip_words)
                if is_skip:
                    # Move to video_check next
                    state["debrief_step"] = "video_check"
                    state["debrief_time"] = datetime.now(TZ)
                    resp.message("you got any video from today? send it or say skip")
                else:
                    state["debrief_step"] = "problem_position"
                    state["debrief_time"] = datetime.now(TZ)
                    resp.message("what position was it?")

            elif step == "problem_position":
                interp = interpret_debrief_reply("problem_position", raw_body)
                if interp.get("unclear"):
                    resp.message(interp.get("ask", "what position specifically?"))
                    return str(resp)
                if interp.get("skip"):
                    state["debrief_step"] = "video_check"
                    state["debrief_time"] = datetime.now(TZ)
                    resp.message("you got any video from today? send it or say skip")
                    return str(resp)
                state["_problem_position"] = interp.get("value", raw_body).strip()
                state["debrief_step"] = "problem_issue"
                state["debrief_time"] = datetime.now(TZ)
                resp.message(f"got it, {state['_problem_position']} — what's the issue with it? where does it break down?")

            elif step == "problem_issue":
                interp = interpret_debrief_reply("problem_issue", raw_body, {
                    "position": state.get("_problem_position", "")
                })
                if interp.get("unclear"):
                    resp.message(interp.get("ask", "describe the issue — where does it break down?"))
                    return str(resp)
                state["_problem_issue"] = interp.get("value", raw_body).strip()
                state["debrief_step"] = "problem_tier"
                state["debrief_time"] = datetime.now(TZ)
                resp.message("how much of a priority is this for Bruno — low, medium, high, or urgent?")

            elif step == "problem_tier":
                raw_tier = raw_body.lower().strip()
                tier_map = {
                    "low": "low", "l": "low",
                    "medium": "med", "med": "med", "m": "med",
                    "high": "high", "h": "high",
                    "urgent": "urgent", "u": "urgent", "asap": "urgent"
                }
                tier = tier_map.get(raw_tier)
                if tier is None:
                    # Didn't understand — re-explain without logging anything
                    resp.message("just reply with one of these: low, medium, high, or urgent — how much of a priority is this for your next Bruno session?")
                    return str(resp)
                position = state.get("_problem_position", "")
                issue = state.get("_problem_issue", "")
                description = issue
                try:
                    with sqlite3.connect(DB_PATH) as conn:
                        conn.execute(
                            'INSERT INTO problems (name, tier, description, created_at) VALUES (?,?,?,?)',
                            (position, tier, description, datetime.now(TZ).isoformat())
                        )
                        conn.commit()
                except Exception as e:
                    logging.error(f"Problem save error: {e}")
                state["_problem_position"] = None
                state["_problem_issue"] = None
                if state.get("intake_mode") == "problem":
                    # Standalone problem report — end here, do not chain into video/injury check
                    state["intake_mode"] = None
                    state["debrief_session"] = None
                    state["debrief_step"] = None
                    state["debrief_headline"] = None
                    state["debrief_time"] = None
                    resp.message(f"flagged — {position} ({tier}) 📌 noted for next time")
                else:
                    # Post-session debrief — continue into the video check
                    state["debrief_step"] = "video_check"
                    state["debrief_time"] = datetime.now(TZ)
                    state["_injury_body_part"] = None
                    state["_injury_severity"] = None
                    state["_injury_description"] = None
                    resp.message(f"flagged — {position} ({tier}) 📌\n\nyou got any video from today? send it or say skip")

            # ── ALLERGY INTAKE (standalone — when user reports symptoms) ───────
            elif step == "intake_allergy_trigger":
                interp = interpret_debrief_reply("intake_allergy_trigger", raw_body)
                if interp.get("unclear"):
                    resp.message(interp.get("ask", "what set it off? (pollen, dust, food, weather, dog, etc.)"))
                    return str(resp)
                state["_allergy_trigger"] = interp.get("value", raw_body).strip()
                state["debrief_step"] = "intake_allergy_symptoms"
                state["debrief_time"] = datetime.now(TZ)
                resp.message("what symptoms are you having? (sneezing, itchy eyes, congestion, hives, shortness of breath, throat tightness, etc.)")

            elif step == "intake_allergy_symptoms":
                interp = interpret_debrief_reply("intake_allergy_symptoms", raw_body)
                if interp.get("unclear"):
                    resp.message(interp.get("ask", "what's bothering you exactly? list anything you're feeling"))
                    return str(resp)
                state["_allergy_symptoms"] = interp.get("value", raw_body).strip()
                state["debrief_step"] = "intake_allergy_severity"
                state["debrief_time"] = datetime.now(TZ)
                resp.message("how bad is it?\n\n• *Mild* — annoying, can train through it\n• *Moderate* — slowing you down\n• *Severe* — really hurting performance\n• *Critical* — can't function, EpiPen / ER territory")

            elif step == "intake_allergy_severity":
                lower = raw_body.lower().strip()
                severity_map = {
                    "mild": "mild", "annoying": "mild", "light": "mild",
                    "moderate": "moderate", "medium": "moderate", "med": "moderate", "okay": "moderate",
                    "severe": "severe", "bad": "severe", "rough": "severe",
                    "critical": "critical", "emergency": "critical", "er": "critical", "anaphylaxis": "critical",
                }
                severity = next((v for k, v in severity_map.items() if k in lower), None)
                if severity is None:
                    resp.message("just pick one: mild, moderate, severe, or critical")
                    return str(resp)
                state["_allergy_severity"] = severity
                state["debrief_step"] = "intake_allergy_medication"
                state["debrief_time"] = datetime.now(TZ)
                resp.message("did you take anything for it? (e.g. Claritin, Zyrtec, Benadryl, inhaler, EpiPen) — or say 'none'")

            elif step == "intake_allergy_medication":
                lower = raw_body.lower().strip()
                if lower in ("none", "no", "nothing", "n/a", "na", "nope", "nah"):
                    state["_allergy_medication"] = None
                else:
                    interp = interpret_debrief_reply("intake_allergy_medication", raw_body)
                    if interp.get("unclear"):
                        resp.message(interp.get("ask", "name the meds or say none"))
                        return str(resp)
                    state["_allergy_medication"] = interp.get("value", raw_body).strip()
                state["debrief_step"] = "intake_allergy_training_impact"
                state["debrief_time"] = datetime.now(TZ)
                resp.message("did this mess with training today?\n\n• *None* — no impact\n• *Reduced* — trained but slowed down\n• *Missed* — skipped a session entirely")

            elif step == "intake_allergy_training_impact":
                lower = raw_body.lower().strip()
                impact = None
                missed = False
                if any(w in lower for w in ("missed", "skipped", "couldn't", "didn't train", "no training")):
                    impact = "missed"
                    missed = True
                elif any(w in lower for w in ("reduced", "slowed", "limited", "less", "half", "lighter", "partial")):
                    impact = "reduced"
                elif any(w in lower for w in ("none", "no", "fine", "normal", "trained", "full")):
                    impact = "none"
                if impact is None:
                    resp.message("just pick one: none / reduced / missed")
                    return str(resp)
                state["_allergy_training_impact"] = impact
                # Save and end
                trigger = state.get("_allergy_trigger", "")
                symptoms = state.get("_allergy_symptoms", "")
                severity = state.get("_allergy_severity", "mild")
                meds = state.get("_allergy_medication")
                # Try to infer category from trigger text
                category = "general"
                trig_lower = (trigger or "").lower()
                if any(w in trig_lower for w in ("pollen", "tree", "grass", "ragweed", "season")):
                    category = "seasonal"
                elif any(w in trig_lower for w in ("dust", "mold", "mildew", "cat", "dog", "pet")):
                    category = "environmental"
                elif any(w in trig_lower for w in ("nut", "peanut", "shellfish", "fish", "egg", "milk", "dairy", "gluten", "wheat", "soy", "food")):
                    category = "food"
                elif any(w in trig_lower for w in ("med", "drug", "antibiotic", "penicillin", "aspirin")):
                    category = "medication"
                save_allergy(
                    trigger=trigger,
                    symptoms=symptoms,
                    severity=severity,
                    category=category,
                    medication=meds,
                    training_impact=impact,
                    missed_training=missed,
                )
                # Clean up
                state["_allergy_trigger"] = None
                state["_allergy_symptoms"] = None
                state["_allergy_severity"] = None
                state["_allergy_medication"] = None
                state["_allergy_training_impact"] = None
                state["_allergy_missed_training"] = None
                state["intake_mode"] = None
                state["debrief_session"] = None
                state["debrief_step"] = None
                state["debrief_time"] = None
                state["debrief_headline"] = None
                emoji = {"mild": "🌼", "moderate": "🤧", "severe": "🚨", "critical": "🆘"}.get(severity, "🤧")
                resp.message(f"logged {trigger} · {severity} {emoji} feel better")

            return str(resp)

        # Streak query
        streak_words = ["streak", "how many days", "water streak", "days in a row"]
        if any(w in body for w in streak_words):
            count = get_streak_count()
            if count == 0:
                resp.message("Your streak is at 0 — hit your 3L goal today to start one 💧")
            elif count == 1:
                resp.message("You're on a 1-day streak 💧 Keep it going!")
            else:
                resp.message(f"You're on a {count}-day streak 🔥💧 Don't break it!")
            return str(resp)

        # Try classifying the message as water / meal / injury logging before falling to chat
        intents = classify_message(raw_body)

        if intents.get("water_l"):
            try:
                liters = float(intents["water_l"])
            except (TypeError, ValueError):
                liters = 0
            if liters > 0:
                check_and_reset_water()
                state["water_today"] = round(state["water_today"] + liters, 2)
                save_water_to_db()
                maybe_increment_streak(liters)
                try:
                    with sqlite3.connect(DB_PATH) as conn:
                        conn.execute(
                            'INSERT INTO water_log (date, time, amount_l, created_at) VALUES (?,?,?,?)',
                            (datetime.now(TZ).strftime('%Y-%m-%d'), datetime.now(TZ).strftime('%H:%M'), liters, datetime.now(TZ).isoformat())
                        )
                        conn.commit()
                except Exception as e:
                    logging.error(f"water_log insert error: {e}")
                remaining = round(WATER_GOAL_L - state["water_today"], 2)
                if remaining <= 0:
                    resp.message(f"+{liters}L logged 💧 LET'S GO — you hit your {WATER_GOAL_L}L goal today 🎉")
                else:
                    resp.message(f"+{liters}L logged 💧 You've had {state['water_today']}L today — {remaining}L to go")
                return str(resp)

        meal = intents.get("meal")
        if meal and meal.get("name"):
            cals = int(meal.get("calories") or 0)
            save_meal(meal["name"], cals, meal.get("kind", "other"), notes=raw_body)
            total = get_today_calories()
            remaining = max(0, CALORIE_GOAL - total)
            resp.message(f"+{cals} cal logged 🍚 ({meal['name']}) · {total:,}/{CALORIE_GOAL:,} today · {remaining} to go")
            return str(resp)

        # ── On-demand structured intake ────────────────────────────────────
        # If the message looks like a NEW injury report, start the full
        # multi-step injury interview (body part → severity → description →
        # partner → when → rest plan). Same fields the dashboard form has.
        injury = intents.get("injury")
        if injury and injury.get("body_part"):
            state["_injury_body_part"] = injury["body_part"]
            # Severity asked next — use the dashboard's vocabulary (managing/watch/fresh/critical)
            state["intake_mode"] = "injury"
            state["debrief_step"] = "injury_severity"
            state["debrief_time"] = datetime.now(TZ)
            resp.message(
                f"got it — {injury['body_part']}. how bad is it?\n\n"
                "• *Managing* — minor, barely noticeable\n"
                "• *Watch* — noticeable, keeping an eye on it\n"
                "• *Fresh* — happened recently, needs attention\n"
                "• *Critical* — serious, may need to stop training"
            )
            return str(resp)

        # If the message reports an allergy flare-up, start the allergy interview
        if intents.get("allergy"):
            state["intake_mode"] = "allergy"
            state["debrief_step"] = "intake_allergy_trigger"
            state["debrief_time"] = datetime.now(TZ)
            resp.message("damn, what set it off? (pollen, dust, food, weather, dog, etc.)")
            return str(resp)

        # If the user spontaneously talks about a Bruno private — start the same
        # debrief flow the post-class checkin uses.
        if intents.get("bruno_lesson"):
            state["debrief_session"] = "Private with Bruno"
            state["debrief_step"] = "headline"
            state["debrief_time"] = datetime.now(TZ)
            state["debrief_headline"] = None
            state["debrief_one_liner"] = None
            resp.message("dope — what'd you and Bruno work on?")
            return str(resp)

        # If the user flags a technique problem, start the problem interview
        prob = intents.get("problem")
        if prob and prob.get("position"):
            state["_problem_position"] = prob["position"]
            state["intake_mode"] = "problem"
            state["debrief_step"] = "problem_issue"
            state["debrief_time"] = datetime.now(TZ)
            hint = (prob.get("hint") or "").strip()
            opener = f"got it, {prob['position']}"
            if hint:
                opener += f" — {hint}"
            opener += ". what's the issue with it? where does it break down?"
            resp.message(opener)
            return str(resp)

        # Nothing structured — full chat
        resp.message(ask_claude(raw_body))

    return str(resp)

# ── HEALTH / HOME ─────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def home():
    return f"Corey's BJJ Assistant is running 🥋\nModel: {CLAUDE_MODEL}", 200

# ── DASHBOARD STATUS API ──────────────────────────────────────────────────────

@app.route('/api/status', methods=['GET'])
def api_status():
    auth = request.headers.get('Authorization', '')
    expected = f"Bearer {os.environ.get('API_TOKEN', '')}"
    if not os.environ.get('API_TOKEN') or auth != expected:
        return {"error": "unauthorized"}, 401
    check_and_reset_water()
    journal = get_recent_journal(10)
    recent_messages = load_chat_history(20)
    cal_today = get_today_calories()
    injuries = get_active_injuries()
    return {
        "model": CLAUDE_MODEL,
        "twilio_configured": bool(ACCOUNT_SID and AUTH_TOKEN),
        "from_number": FROM_NUMBER,
        "my_number": MY_NUMBER,
        # water
        "water_today": state["water_today"],
        "water_goal": WATER_GOAL_L,
        "water_remaining": round(WATER_GOAL_L - state["water_today"], 2),
        "streak_days": get_streak_count(),
        "daily_water_7d": get_water_history(6),
        "water_log_today": get_water_log_today(),
        # bot state
        "last_question": state["last_question"],
        "drilling_time": state["drilling_time"],
        "stretch_time": state["stretch_time"],
        "rest_day": state["rest_day"],
        "awaiting_reply": state["awaiting_reply"],
        "followup_index": state["followup_index"],
        "debrief_session": state["debrief_session"],
        "flag_for_bruno": state["flag_for_bruno"],
        # training
        "next_up": get_next_up(),
        "today_schedule": get_today_schedule(),
        "bruno_recent": get_bruno_recent(),
        "bruno_lessons": get_bruno_lessons(20),
        "problems": (
            [{"name": state["flag_for_bruno"], "tier": "urgent"}]
            if state.get("flag_for_bruno") else []
        ),
        # nutrition
        "calorie_today": cal_today,
        "calorie_goal": CALORIE_GOAL,
        "calorie_remaining": max(0, CALORIE_GOAL - cal_today),
        "recent_meal": get_recent_meal(),
        "meals_today": get_all_meals_today(),
        # injuries
        "injuries_active": injuries,
        "injuries_count": len(injuries),
        "all_injuries": get_all_injuries(),
        "injury_stats": get_injury_stats(),
        "allergies": get_all_allergies(),
        "allergy_stats": get_allergy_stats(),
        "all_problems": get_all_problems(),
        "techniques": get_all_techniques(),
        # logs
        "recent_messages": recent_messages,
        "journal": [
            {"date": d, "session": s, "notes": n}
            for d, s, n in journal
        ],
    }

# ── NOTIFY (push WhatsApp message via API token) ──────────────────────────────

@app.route('/api/notify', methods=['POST'])
def api_notify():
    auth = request.headers.get('Authorization', '')
    expected = f"Bearer {os.environ.get('API_TOKEN', '')}"
    if not os.environ.get('API_TOKEN') or auth != expected:
        return {"error": "unauthorized"}, 401
    payload = request.get_json(silent=True) or {}
    msg = (payload.get('message') or '').strip()
    if not msg:
        return {"error": "empty message"}, 400
    result = send(msg, followup=False)
    if not result.get("ok"):
        return result, 503
    return result

@app.route('/api/twilio/message/<sid>', methods=['GET'])
def api_twilio_message_status(sid):
    auth = request.headers.get('Authorization', '')
    expected = f"Bearer {os.environ.get('API_TOKEN', '')}"
    if not os.environ.get('API_TOKEN') or auth != expected:
        return {"error": "unauthorized"}, 401
    if not ACCOUNT_SID or not AUTH_TOKEN:
        return {"ok": False, "error": "twilio_credentials_missing"}, 503
    try:
        message = client.messages(sid).fetch()
        return {
            "ok": True,
            "sid": message.sid,
            "status": message.status,
            "error_code": message.error_code,
            "error_message": message.error_message,
            "to": message.to,
            "from_number": getattr(message, "from_", None),
            "date_created": message.date_created.isoformat() if message.date_created else None,
            "date_sent": message.date_sent.isoformat() if message.date_sent else None,
        }
    except Exception as e:
        logging.error(f"Twilio status lookup error ({sid}): {e}")
        return {"ok": False, "error": "twilio_status_lookup_failed", "detail": str(e)}, 502

# ── TRIGGER (for testing) ─────────────────────────────────────────────────────

@app.route('/trigger/<action>', methods=['GET'])
def trigger(action):
    actions = {
        "drilling": ask_drilling_time,
        "stretch": ask_stretch_time,
        "water": water_morning,
        "sc": remind_sc,
        "private": remind_private,
        "evening": remind_evening,
        "partner": ask_partner_drilling,
        "penalty": water_penalty_check,
        "checkin_private": checkin_after_private,
        "checkin_drilling": checkin_after_drilling,
        "checkin_evening": checkin_after_evening,
    }
    fn = actions.get(action)
    if fn:
        fn()
        return f"Triggered: {action}", 200
    return f"Unknown action: {action}. Options: {', '.join(actions.keys())}", 400

# ── SCHEDULER ─────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone=TZ)

# Ask drilling time every weekday morning
scheduler.add_job(ask_drilling_time, 'cron', day_of_week='mon,wed,fri', hour=6, minute=30)

# Ask stretch zone time on Tue/Thu
scheduler.add_job(ask_stretch_time, 'cron', day_of_week='tue,thu', hour=10, minute=30)

# S&C with Roy (Mon, Wed, Fri)
scheduler.add_job(remind_sc,       'cron', day_of_week='mon,wed,fri', hour=9,  minute=45)
scheduler.add_job(checkin_after_sc,'cron', day_of_week='mon,wed,fri', hour=11, minute=5)

# Private with Bruno (Mon–Fri)
scheduler.add_job(remind_private,       'cron', day_of_week='mon-fri', hour=13, minute=45)
scheduler.add_job(checkin_after_private,'cron', day_of_week='mon-fri', hour=16, minute=15)

# Evening class — Mon, Tue, Thu
scheduler.add_job(remind_evening,       'cron', day_of_week='mon,tue,thu', hour=19, minute=30)
scheduler.add_job(checkin_after_evening,'cron', day_of_week='mon,tue,thu', hour=21, minute=5)

# Ask partners about drilling (Sun–Thu at 7 PM, for next morning)
scheduler.add_job(ask_partner_drilling, 'cron', day_of_week='sun,mon,tue,wed,thu', hour=19, minute=0)

# Water penalty check — 10 PM daily
scheduler.add_job(water_penalty_check, 'cron', hour=22, minute=0)

# Water reminders — every day
scheduler.add_job(water_late_night,'cron', hour=1,  minute=39)
scheduler.add_job(water_morning,   'cron', hour=9,  minute=0)
scheduler.add_job(water_afternoon, 'cron', hour=13, minute=30)
scheduler.add_job(water_evening,   'cron', hour=19, minute=5)

init_db()
load_water_from_db()
chat_history.extend(load_chat_history(20))
scheduler.start()

# ── CRUD ENDPOINTS ────────────────────────────────────────────────────────────

@app.route('/api/meals', methods=['POST'])
def api_meals_create():
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    data = request.get_json(force=True) or {}
    save_meal(data.get('name', ''), data.get('calories', 0), data.get('kind', 'other'), data.get('notes', ''))
    return {"ok": True}

@app.route('/api/meals/<int:meal_id>', methods=['PATCH'])
def api_meals_update(meal_id):
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    data = request.get_json(force=True) or {}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            for field in ('name', 'kind', 'time', 'notes'):
                if field in data:
                    conn.execute(f'UPDATE meals SET {field}=? WHERE id=?', (data[field], meal_id))
            if 'calories' in data:
                conn.execute('UPDATE meals SET calories=? WHERE id=?', (int(data['calories']), meal_id))
            conn.commit()
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route('/api/meals/<int:meal_id>', methods=['DELETE'])
def api_meals_delete(meal_id):
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('DELETE FROM meals WHERE id=?', (meal_id,))
            conn.commit()
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route('/api/injuries', methods=['POST'])
def api_injuries_create():
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    data = request.get_json(force=True) or {}
    body_part = (data.get('body_part') or '').strip()
    if not body_part:
        return {"error": "body_part required"}, 400
    severity = (data.get('severity') or 'minor').strip()
    notes = data.get('notes', '')
    partner = data.get('partner') or None
    when_happened = data.get('when_happened') or None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            now = datetime.now(TZ)
            cursor = conn.execute(
                'INSERT INTO injuries (date, body_part, severity, notes, partner, when_happened, created_at) VALUES (?,?,?,?,?,?,?)',
                (now.strftime('%Y-%m-%d'), body_part, severity, notes, partner, when_happened, now.isoformat())
            )
            conn.commit()
        return {"ok": True, "id": cursor.lastrowid, "date": now.strftime('%Y-%m-%d'), "created_at": now.isoformat()}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route('/api/injuries/<int:injury_id>', methods=['PATCH'])
def api_injuries_update(injury_id):
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    data = request.get_json(force=True) or {}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            exists = conn.execute('SELECT 1 FROM injuries WHERE id=?', (injury_id,)).fetchone()
            if not exists:
                return {"error": "injury not found"}, 404
            changed = 0
            for field in ('body_part', 'severity', 'notes', 'partner', 'when_happened'):
                if field in data:
                    changed += conn.execute(f'UPDATE injuries SET {field}=? WHERE id=?', (data[field], injury_id)).rowcount
            if 'resolved' in data:
                changed += conn.execute('UPDATE injuries SET resolved=? WHERE id=?', (1 if data['resolved'] else 0, injury_id)).rowcount
            conn.commit()
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route('/api/injuries/<int:injury_id>', methods=['DELETE'])
def api_injuries_delete(injury_id):
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    try:
        with sqlite3.connect(DB_PATH) as conn:
            changed = conn.execute('DELETE FROM injuries WHERE id=?', (injury_id,)).rowcount
            conn.commit()
        if changed == 0:
            return {"error": "injury not found"}, 404
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}, 500


@app.route('/api/allergies', methods=['POST'])
def api_allergies_create():
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    data = request.get_json(force=True) or {}
    category = (data.get('category') or 'general').strip()
    symptoms = (data.get('symptoms') or '').strip()
    trigger_name = (data.get('trigger') or '').strip()
    if not symptoms and not trigger_name:
        return {"error": "symptoms or trigger required"}, 400
    now = datetime.now(TZ)
    date = (data.get('date') or now.strftime('%Y-%m-%d')).strip()
    time_text = (data.get('time') or now.strftime('%H:%M')).strip()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.execute(
                'INSERT INTO allergies (date, time, severity, category, trigger_name, symptoms, medication, training_impact, missed_training, created_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?)',
                (
                    date,
                    time_text,
                    (data.get('severity') or 'mild').strip(),
                    category or 'general',
                    trigger_name,
                    symptoms,
                    (data.get('medication') or '').strip(),
                    (data.get('training_impact') or 'none').strip(),
                    1 if data.get('missed_training') else 0,
                    now.isoformat(),
                )
            )
            conn.commit()
        return {"ok": True, "id": cursor.lastrowid, "date": date, "created_at": now.isoformat()}
    except Exception as e:
        return {"error": str(e)}, 500


@app.route('/api/allergies/<int:allergy_id>', methods=['PATCH'])
def api_allergies_update(allergy_id):
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    data = request.get_json(force=True) or {}
    allowed = ('date', 'time', 'severity', 'category', 'symptoms', 'medication', 'training_impact')
    try:
        with sqlite3.connect(DB_PATH) as conn:
            exists = conn.execute('SELECT 1 FROM allergies WHERE id=?', (allergy_id,)).fetchone()
            if not exists:
                return {"error": "allergy log not found"}, 404
            changed = 0
            for field in allowed:
                if field in data:
                    changed += conn.execute(f'UPDATE allergies SET {field}=? WHERE id=?', ((data.get(field) or '').strip(), allergy_id)).rowcount
            if 'trigger' in data:
                changed += conn.execute('UPDATE allergies SET trigger_name=? WHERE id=?', ((data.get('trigger') or '').strip(), allergy_id)).rowcount
            if 'missed_training' in data:
                changed += conn.execute('UPDATE allergies SET missed_training=? WHERE id=?', (1 if data['missed_training'] else 0, allergy_id)).rowcount
            conn.commit()
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}, 500


@app.route('/api/allergies/<int:allergy_id>', methods=['DELETE'])
def api_allergies_delete(allergy_id):
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    try:
        with sqlite3.connect(DB_PATH) as conn:
            changed = conn.execute('DELETE FROM allergies WHERE id=?', (allergy_id,)).rowcount
            conn.commit()
        if changed == 0:
            return {"error": "allergy log not found"}, 404
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route('/api/problems', methods=['POST'])
def api_problems_create():
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    data = request.get_json(force=True) or {}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                'INSERT INTO problems (name, tier, description, created_at) VALUES (?,?,?,?)',
                (data.get('name', ''), data.get('tier', 'med'), data.get('description', ''), datetime.now(TZ).isoformat())
            )
            conn.commit()
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route('/api/problems/<int:problem_id>', methods=['PATCH'])
def api_problems_update(problem_id):
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    data = request.get_json(force=True) or {}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            for field in ('name', 'tier', 'description'):
                if field in data:
                    conn.execute(f'UPDATE problems SET {field}=? WHERE id=?', (data[field], problem_id))
            if 'resolved' in data:
                conn.execute('UPDATE problems SET resolved=? WHERE id=?', (1 if data['resolved'] else 0, problem_id))
            conn.commit()
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route('/api/problems/<int:problem_id>', methods=['DELETE'])
def api_problems_delete(problem_id):
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('DELETE FROM problems WHERE id=?', (problem_id,))
            conn.commit()
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route('/api/water/add', methods=['POST'])
def api_water_add():
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    data = request.get_json(force=True) or {}
    try:
        liters = float(data.get('amount_l', 0))
    except (TypeError, ValueError):
        return {"error": "invalid amount_l"}, 400
    if liters <= 0:
        return {"error": "amount_l must be > 0"}, 400
    now = datetime.now(TZ)
    check_and_reset_water()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                'INSERT INTO water_log (date, time, amount_l, created_at) VALUES (?,?,?,?)',
                (now.strftime('%Y-%m-%d'), now.strftime('%H:%M'), liters, now.isoformat())
            )
            conn.commit()
        state["water_today"] = round(state["water_today"] + liters, 2)
        save_water_to_db()
        maybe_increment_streak(liters)
        return {"ok": True, "water_today": state["water_today"]}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route('/api/water/entry/<int:entry_id>', methods=['DELETE'])
def api_water_delete(entry_id):
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute('SELECT amount_l FROM water_log WHERE id=?', (entry_id,)).fetchone()
            if not row:
                return {"error": "not found"}, 404
            conn.execute('DELETE FROM water_log WHERE id=?', (entry_id,))
            conn.commit()
        state["water_today"] = round(max(0.0, state["water_today"] - row[0]), 2)
        save_water_to_db()
        return {"ok": True, "water_today": state["water_today"]}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route('/api/technique-history', methods=['GET'])
def api_technique_history():
    auth = request.headers.get('Authorization', '')
    expected = f"Bearer {os.environ.get('API_TOKEN', '')}"
    if not os.environ.get('API_TOKEN') or auth != expected:
        return {"error": "unauthorized"}, 401
    try:
        with sqlite3.connect(DB_PATH) as conn:
            techniques = conn.execute(
                'SELECT id, name, summary FROM techniques ORDER BY created_at DESC'
            ).fetchall()
        result = []
        for tid, name, summary in techniques:
            with sqlite3.connect(DB_PATH) as conn:
                logs = conn.execute(
                    'SELECT date, session, notes, sentiment, video_url FROM technique_log '
                    'WHERE technique_id = ? ORDER BY created_at ASC',
                    (tid,)
                ).fetchall()
            result.append({
                "id": tid,
                "name": name,
                "summary": summary,
                "entries": [
                    {"date": r[0], "session": r[1], "notes": r[2], "sentiment": r[3], "video_url": r[4]}
                    for r in logs
                ]
            })
        return {"techniques": result}
    except Exception as e:
        return {"error": str(e)}, 500

# ── RUN ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
