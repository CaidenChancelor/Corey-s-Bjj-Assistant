import os
import base64
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

client = Client(ACCOUNT_SID, AUTH_TOKEN)
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None
CLAUDE_MODEL = "claude-sonnet-4-5"
WATER_GOAL_L = 3.0
TZ = pytz.timezone('America/New_York')

# Chat history for conversational context
chat_history = []

SYSTEM_PROMPT = """You are Corey's personal BJJ training assistant texting him on WhatsApp. You talk like a real friend — casual, supportive, sometimes funny. Keep messages SHORT (1-3 sentences max, this is texting not email).

You know Corey's full schedule:
- Mon/Wed/Fri: Drilling (7-8 or 8-9 AM), S&C with Roy (10-11 AM), Private with Bruno Malfacine (2-4 PM)
- Mon/Tue/Thu: Evening class with Bruno (7:45-9 PM)
- Tue/Thu: Stretch Zone (11-12 or 12-1 PM), Private with Bruno (2-4 PM), Competition Class with Bruno (7:45-9 PM)
- Every day: Drink a gallon of water

You care about his training, recovery, hydration, and mindset. You're like a coach/homie hybrid. Don't be robotic. Use slang naturally. You can use emojis but don't overdo it."""

def ask_claude(user_msg):
    if not claude:
        return "Bot brain offline — API key missing"
    chat_history.append({"role": "user", "content": user_msg})
    # Keep last 20 messages for context
    if len(chat_history) > 20:
        chat_history.pop(0)
    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=chat_history,
        )
    except Exception as e:
        chat_history.pop()  # roll back user message on failure
        logging.error(f"Claude API error: {e}")
        return "Yo my bad, brain glitched for a sec. Say that again?"
    reply = response.content[0].text
    chat_history.append({"role": "assistant", "content": reply})
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
    "last_question": None,   # what we're waiting on
    "drilling_time": None,   # 7 or 8
    "stretch_time": None,    # 11 or 12
    "awaiting_reply": False, # follow-up tracking
    "partner_pending": None, # waiting on partner reply
    "replying_to": None,     # which partner Corey is responding to
    "followup_index": 0,     # how many follow-ups have fired
    "followup_delays": [],   # cadence for current follow-up chain
    "water_today": 0.0,      # liters consumed today
    "water_date": None,      # "YYYY-MM-DD" — resets daily
}

# Schedule follow-ups: every 15 min, 3 times
FOLLOWUP_DELAYS_MIN  = [15, 30, 45]
# Water follow-ups: every 5 min, 2 times
WATER_FOLLOWUP_DELAYS = [5, 10]


# ── WATER TRACKING ────────────────────────────────────────────────────────────

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
                            "This is a photo of a water bottle. Estimate how many liters of water "
                            "the person has CONSUMED/FINISHED from it. "
                            "Empty bottle = 1.0L consumed. Half empty = 0.5L consumed. "
                            "Reply with ONLY a decimal number like 0.5 or 1.0. Nothing else."
                        )
                    }
                ]
            }]
        )
        return float(response.content[0].text.strip())
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
    client.messages.create(body=msg, from_=FROM_NUMBER, to=number)
    logging.info(f"SENT to {number}: {msg}")

def send(msg, followup=False, delays=None):
    client.messages.create(body=msg, from_=FROM_NUMBER, to=MY_NUMBER)
    logging.info(f"SENT: {msg}")
    if followup:
        state["awaiting_reply"] = True
        state["followup_index"] = 0
        state["followup_delays"] = delays or FOLLOWUP_DELAYS_MIN
        run_at = datetime.now(TZ) + timedelta(minutes=state["followup_delays"][0])
        scheduler.add_job(send_followup, 'date', run_date=run_at,
                          id='followup', replace_existing=True)

def send_water(msg):
    send(msg, followup=True, delays=WATER_FOLLOWUP_DELAYS)

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
    send("Yo what time you drilling this morning — 7 or 8?", followup=True)

def ask_stretch_time():
    state["stretch_time"] = None
    state["last_question"] = "stretch_time"
    send("Stretch Zone today — 11 or 12?", followup=True)

def checkin_after_drilling():
    send("Drilling done? How'd it feel 👊")

def remind_sc():
    send("S&C with Roy in 15 — you ready to suffer lol")
    send_water("You got your water for S&C? 💧")

def checkin_after_sc():
    send("You make it through Roy today? 💀")

def remind_private():
    send("Bruno private in 15 — get your head right 🥋")
    send_water("Water before the private 💧")

def checkin_after_private():
    send("How was the private? What'd you work on?")

def remind_stretch():
    send("Stretch Zone coming up — you heading out?")
    send_water("Bring that water to Stretch Zone 💧")

def checkin_after_stretch():
    send("Body feeling better after Stretch Zone?")

def remind_evening():
    send("Evening class with Bruno at 7:45 — you on your way?")
    send_water("Sip that water before you head out 💧")

def checkin_after_evening():
    send("How was tonight? What'd Bruno have you drilling?")

# ── PARTNER MESSAGES ──────────────────────────────────────────────────────────

def ask_partner_drilling():
    for name, number in PARTNERS.items():
        send_to(number, f"Hey what's up {name}! How you doing big man? Just wanted to confirm for drilling tomorrow — is it from 7 to 8 or 8 to 9?")
        state["partner_pending"] = name
    logging.info("Asked partners about drilling")

def water_late_night():
    send_water("Yo it's late — you still drinking water or nah?")

def water_morning():
    send_water("You sipping on that water yet? Start early 💧")

def water_afternoon():
    send_water("Mid-day check — how's that gallon looking?")

def water_evening():
    send_water("Almost end of day — you hit that gallon?")

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
        if liters is not None:
            check_and_reset_water()
            state["water_today"] = round(state["water_today"] + liters, 2)
            remaining = round(WATER_GOAL_L - state["water_today"], 2)
            if remaining <= 0:
                resp.message(f"LET'S GO!! You hit your {WATER_GOAL_L}L goal today 🎉💧")
            else:
                resp.message(f"+{liters}L logged 💧 You've had {state['water_today']}L today — {remaining}L to go")
        else:
            resp.message("Couldn't read that — just text me how many liters (e.g. '1' or '0.5')")
        return str(resp)

    # Check if message is from a partner — relay to Corey
    partner_name = None
    for name, number in PARTNERS.items():
        if sender == number:
            partner_name = name
            break

    if partner_name:
        # Forward partner's reply to Corey — nag him to respond
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
        if "7" in body:
            state["drilling_time"] = 7
            state["last_question"] = None
            resp.message("Bet — drilling at 7. I'll check in after 🔥")
            send_water("And start sipping that water now 💧")
            run_at = datetime.now(TZ).replace(hour=8, minute=5, second=0, microsecond=0)
            if datetime.now(TZ) < run_at:
                scheduler.add_job(checkin_after_drilling, 'date', run_date=run_at,
                                  id='drill_checkin', replace_existing=True)
        elif "8" in body:
            state["drilling_time"] = 8
            state["last_question"] = None
            resp.message("Bet — drilling at 8. Got you 👊")
            send_water("And start sipping that water now 💧")
            run_at = datetime.now(TZ).replace(hour=9, minute=5, second=0, microsecond=0)
            if datetime.now(TZ) < run_at:
                scheduler.add_job(checkin_after_drilling, 'date', run_date=run_at,
                                  id='drill_checkin', replace_existing=True)
        elif claude_is_skip(raw_body, "Yo what time you drilling this morning — 7 or 8?"):
            state["last_question"] = None
            state["drilling_time"] = None
            resp.message(ask_claude(raw_body))
        else:
            resp.message("Just say 7 or 8 lol")

    elif last_q == "stretch_time":
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
            resp.message("Just say 11 or 12")

    else:
        # No structured question pending — use Claude for conversation
        resp.message(ask_claude(raw_body))

    return str(resp)

# ── HEALTH / HOME ─────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def home():
    return "Corey's BJJ Assistant is running 🥋", 200

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
    }
    fn = actions.get(action)
    if fn:
        fn()
        return f"Triggered: {action}", 200
    return f"Unknown action: {action}. Options: {', '.join(actions.keys())}", 400

# ── SCHEDULER ─────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone=TZ)

# Ask drilling time every weekday morning
scheduler.add_job(ask_drilling_time, 'cron', day_of_week='mon-fri', hour=6, minute=30)

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

# Water reminders — every day
scheduler.add_job(water_late_night,'cron', hour=1,  minute=39)
scheduler.add_job(water_morning,   'cron', hour=9,  minute=0)
scheduler.add_job(water_afternoon, 'cron', hour=13, minute=30)
scheduler.add_job(water_evening,   'cron', hour=19, minute=0)

scheduler.start()

# ── RUN ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
