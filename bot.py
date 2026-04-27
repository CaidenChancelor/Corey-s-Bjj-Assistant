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
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            date       TEXT,
            body_part  TEXT,
            severity   TEXT,
            notes      TEXT,
            resolved   INTEGER DEFAULT 0,
            created_at TEXT
        )''')
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


def classify_message(text):
    """One Claude call → structured intents. Returns dict with water_l, meal, injury keys."""
    empty = {"water_l": None, "meal": None, "injury": None}
    if not claude or not text:
        return empty
    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            system=(
                "You are an intent classifier for a BJJ training assistant. "
                "Given a user message, output JSON with these fields:\n"
                '- water_l: number of liters of water if user is logging water intake, else null. '
                'Examples: "drank 1L"→1.0, "had 500ml"→0.5, "just finished a glass"→0.25, '
                '"chugged a bottle"→0.5, "drank 2 bottles of water"→1.0.\n'
                '- meal: object {name, calories, kind} if user is logging a meal/food, else null. '
                'kind ∈ {"breakfast","lunch","dinner","snack","pre-training","post-training","other"}. '
                'Estimate calories (integer) if not given.\n'
                '- injury: object {body_part, severity, notes} if user is reporting an injury, tweak, or pain, else null. '
                'severity ∈ {"minor","moderate","severe"}. Body part should be specific (e.g. "left knee", "lower back").\n\n'
                "Return ONLY valid JSON. No markdown, no explanation."
            ),
            messages=[{"role": "user", "content": text}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r'^```\w*\s*|\s*```$', '', raw).strip()
        data = json.loads(raw)
        # Normalize keys we expect
        return {
            "water_l": data.get("water_l"),
            "meal": data.get("meal"),
            "injury": data.get("injury"),
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
    """Return all injuries (active + resolved) for the dashboard Micro-Injuries page."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT id, date, body_part, severity, notes, resolved FROM injuries ORDER BY created_at DESC LIMIT 20'
            ).fetchall()
        return [{"id": r[0], "date": r[1], "body_part": r[2], "severity": r[3], "notes": r[4], "resolved": bool(r[5])} for r in rows]
    except Exception:
        return []


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
        state["water_today"] = 0.0
        state["water_date"] = today

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
    try:
        client.messages.create(body=msg, from_=FROM_NUMBER, to=number)
        logging.info(f"SENT to {number}: {msg}")
    except Exception as e:
        logging.error(f"send_to error ({number}): {e}")

def send(msg, followup=False, delays=None):
    try:
        client.messages.create(body=msg, from_=FROM_NUMBER, to=MY_NUMBER)
        logging.info(f"SENT: {msg}")
    except Exception as e:
        logging.error(f"send error: {e}")
    if followup:
        state["awaiting_reply"] = True
        state["followup_index"] = 0
        state["followup_delays"] = delays or FOLLOWUP_DELAYS_MIN
        run_at = datetime.now(TZ) + timedelta(minutes=state["followup_delays"][0])
        scheduler.add_job(send_followup, 'date', run_date=run_at,
                          id='followup', replace_existing=True)

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
    send("Drilling done? How'd it feel — what were you working on?")

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
    send("How was the private? What'd you work on?")

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
    send("How was tonight? What'd Bruno have you drilling?")

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
            run_at = datetime.now(TZ).replace(hour=8, minute=5, second=0, microsecond=0)
            if datetime.now(TZ) < run_at:
                scheduler.add_job(checkin_after_drilling, 'date', run_date=run_at,
                                  id='drill_checkin', replace_existing=True)
        elif "8" in body:
            state["drilling_time"] = 8
            state["last_question"] = None
            resp.message("Bet — drilling at 8. Got you 👊")
            send_water("Start sipping now —")
            run_at = datetime.now(TZ).replace(hour=9, minute=5, second=0, microsecond=0)
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
        # If Corey is debriefing a session, journal it and extract any technique struggle
        if state.get("debrief_session"):
            _dbt = state.get("debrief_time")
            if _dbt and (datetime.now(TZ) - _dbt).total_seconds() > 14400:
                # Debrief question is over 4 hours old — they probably skipped, don't journal
                state["debrief_session"] = None
                state["debrief_time"] = None
                resp.message(ask_claude(raw_body))
                return str(resp)
            session = state["debrief_session"]
            state["debrief_session"] = None
            state["debrief_time"] = None
            save_journal_entry(session, raw_body)
            issue = extract_issue(raw_body)
            if issue:
                state["flag_for_bruno"] = issue
            resp.message(ask_claude(raw_body))
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

        injury = intents.get("injury")
        if injury and injury.get("body_part"):
            sev = injury.get("severity", "minor")
            notes = injury.get("notes") or raw_body
            save_injury(injury["body_part"], sev, notes)
            emoji = {"minor": "🩹", "moderate": "🩼", "severe": "🚨"}.get(sev, "🩹")
            resp.message(f"Logged {injury['body_part']} ({sev}) {emoji} Take it easy bro — anything I should flag for tomorrow?")
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
        # water
        "water_today": state["water_today"],
        "water_goal": WATER_GOAL_L,
        "water_remaining": round(WATER_GOAL_L - state["water_today"], 2),
        "streak_days": get_streak_days(),
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
        "all_problems": get_all_problems(),
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
    send(msg, followup=False)
    return {"ok": True}

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
scheduler.add_job(checkin_after_private,'cron', day_of_week='mon-fri', hour=16, minute=5)

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
    save_injury(data.get('body_part', ''), data.get('severity', 'minor'), data.get('notes', ''))
    return {"ok": True}

@app.route('/api/injuries/<int:injury_id>', methods=['PATCH'])
def api_injuries_update(injury_id):
    auth = request.headers.get('Authorization', '')
    if not os.environ.get('API_TOKEN') or auth != f"Bearer {os.environ.get('API_TOKEN')}":
        return {"error": "unauthorized"}, 401
    data = request.get_json(force=True) or {}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            for field in ('body_part', 'severity', 'notes'):
                if field in data:
                    conn.execute(f'UPDATE injuries SET {field}=? WHERE id=?', (data[field], injury_id))
            if 'resolved' in data:
                conn.execute('UPDATE injuries SET resolved=? WHERE id=?', (1 if data['resolved'] else 0, injury_id))
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
            conn.execute('DELETE FROM injuries WHERE id=?', (injury_id,))
            conn.commit()
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

# ── RUN ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
