import os
from datetime import datetime
import pytz
from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from apscheduler.schedulers.background import BackgroundScheduler
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
AUTH_TOKEN  = os.environ.get('TWILIO_AUTH_TOKEN')
FROM_NUMBER = 'whatsapp:+14155238886'  # Twilio sandbox
MY_NUMBER   = 'whatsapp:+13054601000'

client = Client(ACCOUNT_SID, AUTH_TOKEN)
TZ = pytz.timezone('America/New_York')

# Conversation state (in-memory, resets on redeploy — fine for now)
state = {
    "last_question": None,   # what we're waiting on
    "drilling_time": None,   # 7 or 8
    "stretch_time": None,    # 11 or 12
}

# ── SEND ──────────────────────────────────────────────────────────────────────

def send(msg):
    client.messages.create(body=msg, from_=FROM_NUMBER, to=MY_NUMBER)
    logging.info(f"SENT: {msg}")

# ── SCHEDULED MESSAGES ────────────────────────────────────────────────────────

def ask_drilling_time():
    state["drilling_time"] = None
    state["last_question"] = "drilling_time"
    send("Yo what time you drilling this morning — 7 or 8?")

def ask_stretch_time():
    state["stretch_time"] = None
    state["last_question"] = "stretch_time"
    send("Stretch Zone today — 11 or 12?")

def checkin_after_drilling():
    send("Drilling done? How'd it feel 👊")

def remind_sc():
    send("S&C with Roy in 15 — you ready to suffer lol")

def checkin_after_sc():
    send("You make it through Roy today? 💀")

def remind_private():
    send("Bruno private in 15 — get your head right 🥋")

def checkin_after_private():
    send("How was the private? What'd you work on?")

def remind_stretch():
    send("Stretch Zone coming up — you heading out?")

def checkin_after_stretch():
    send("Body feeling better after Stretch Zone?")

def remind_evening():
    send("Evening class with Bruno at 7:45 — you on your way?")

def checkin_after_evening():
    send("How was tonight? What'd Bruno have you drilling?")

def water_morning():
    send("You sipping on that water yet? Start early 💧")

def water_afternoon():
    send("Mid-day check — how's that gallon looking?")

def water_evening():
    send("Almost end of day — you hit that gallon?")

# ── WEBHOOK (incoming replies from you) ───────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook():
    body = request.form.get('Body', '').strip().lower()
    resp = MessagingResponse()
    last_q = state.get("last_question")

    if last_q == "drilling_time":
        if "7" in body:
            state["drilling_time"] = 7
            state["last_question"] = None
            resp.message("Bet — drilling at 7. I'll check in after 🔥")
            # One-shot checkin after drilling ends
            run_at = datetime.now(TZ).replace(hour=8, minute=5, second=0, microsecond=0)
            if datetime.now(TZ) < run_at:
                scheduler.add_job(checkin_after_drilling, 'date', run_date=run_at,
                                  id='drill_checkin', replace_existing=True)
        elif "8" in body:
            state["drilling_time"] = 8
            state["last_question"] = None
            resp.message("Bet — drilling at 8. Got you 👊")
            run_at = datetime.now(TZ).replace(hour=9, minute=5, second=0, microsecond=0)
            if datetime.now(TZ) < run_at:
                scheduler.add_job(checkin_after_drilling, 'date', run_date=run_at,
                                  id='drill_checkin', replace_existing=True)
        else:
            resp.message("Just say 7 or 8 lol")

    elif last_q == "stretch_time":
        if "11" in body:
            state["stretch_time"] = 11
            state["last_question"] = None
            resp.message("Got it — Stretch Zone at 11 🙆")
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
            remind_at  = datetime.now(TZ).replace(hour=11, minute=45, second=0, microsecond=0)
            checkin_at = datetime.now(TZ).replace(hour=13, minute=5,  second=0, microsecond=0)
            if datetime.now(TZ) < remind_at:
                scheduler.add_job(remind_stretch, 'date', run_date=remind_at,
                                  id='stretch_remind', replace_existing=True)
            if datetime.now(TZ) < checkin_at:
                scheduler.add_job(checkin_after_stretch, 'date', run_date=checkin_at,
                                  id='stretch_checkin', replace_existing=True)
        else:
            resp.message("Just say 11 or 12")

    # If no active question, just acknowledge
    return str(resp)

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

# Water reminders — every day
scheduler.add_job(water_morning,   'cron', hour=9,  minute=0)
scheduler.add_job(water_afternoon, 'cron', hour=13, minute=30)
scheduler.add_job(water_evening,   'cron', hour=19, minute=0)

scheduler.start()

# ── RUN ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
