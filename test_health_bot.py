"""Offline tests for health checks, the notifier's failure handling and the Telegram bot."""
import json
import os
import subprocess
import sys
import tempfile
import time

import store
from health import Health
from notifier import Notifier
from telegram_bot import Bot, chunks

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- health
t = [1000.0]
h = Health({"feed_silence_minutes": 10, "disconnect_alert_minutes": 5, "send_failures_before_alert": 3},
           clock=lambda: t[0])
assert h.problems() == {}
t[0] += 299
assert not h.problems(), "inside the startup grace period"
t[0] += 2
assert "disconnected" in h.problems()
h.on_connect()
assert not h.problems()
t[0] += 599
assert not h.problems()
t[0] += 2
assert "silent" in h.problems(), "connected but quiet for 10 min"
h.on_message()
assert not h.problems()
h.on_feed_error("Api Key Is Not Valid")
assert "feed_error" in h.problems()
h.on_message()
assert "feed_error" not in h.problems(), "real data clears a feed error"
h.on_disconnect()
t[0] += 301
assert "disconnected" in h.problems()
h.on_connect()
t[0] += 601
assert "silent" in h.problems(), "old messages from before the reconnect must not count"
h.on_message()
for _ in range(2):
    h.on_send_result("email", False, "boom")
assert not h.problems()
h.on_send_result("email", False, "boom")
assert "send:email" in h.problems()
h.on_send_result("email", True)
assert not h.problems(), "one success clears it"
snap = h.snapshot(vessels=4)
assert snap["ok"] and snap["vessels"] == 4
print("health OK")

# ----------------------------------------------- status file + docker healthcheck
tmp = tempfile.mkdtemp()
hf = os.path.join(tmp, "health.json")
h.write_status(hf, h.snapshot())
env = dict(os.environ, HEALTH_FILE=hf)
run = lambda: subprocess.run([sys.executable, os.path.join(HERE, "healthcheck.py")], env=env,
                             capture_output=True, text=True).returncode
real = time.time()
good = {"updated_at": real, "ok": True, "problems": []}
json.dump(good, open(hf, "w"))
assert run() == 0
json.dump(dict(good, ok=False, problems=["x: broken"]), open(hf, "w"))
assert run() == 1
json.dump(dict(good, updated_at=real - 200), open(hf, "w"))
assert run() == 1, "stale file means the tracker stopped"
os.remove(hf)
assert run() == 1
print("healthcheck OK")

# ----------------------------------------- notifier: admin routing and probes
contacts = {
    "boss": {"name": "Boss", "role": "other", "email": "b@x.com", "telegram_chat_id": "9", "ntfy_topic": "t",
             "channels": ["email", "telegram", "ntfy"], "health_alerts": True},
    "capt": {"name": "Capt", "role": "port_captain", "email": "c@x.com", "channels": ["email"]},
}
nt = Notifier({"timezone": "America/Los_Angeles"}, contacts)
sent = []
nt._email = lambda c, s, b: sent.append(("email", c["email"], s))
nt._telegram = lambda c, s, b: sent.append(("telegram", c["telegram_chat_id"], s))
nt._ntfy = lambda c, s, b: sent.append(("ntfy", c["ntfy_topic"], s))
n = nt.send_admin("PROBLEM: x", "details", exclude=("email",))
assert n == 2 and {s[0] for s in sent} == {"telegram", "ntfy"}, sent
assert all(s[2].startswith("[Vessel alerts] ") for s in sent)
assert nt.send_admin("hi", "body") == 3
sent.clear()

health = Health({"send_failures_before_alert": 3})
nt.on_result = health.on_send_result
state = {"email_ok": False}


def probe_email():
    if not state["email_ok"]:
        raise RuntimeError("login refused")


nt._probe_email = probe_email
nt._probe_telegram = lambda: None
nt._probe_ntfy = lambda: None
for _ in range(3):
    nt.probe_all()
p = health.problems()
assert list(p) == ["send:email"] and "login refused" in p["send:email"][1], p
state["email_ok"] = True
nt.probe_all()
assert not health.problems()
# a real send that raises is counted too
nt._email = lambda c, s, b: (_ for _ in ()).throw(OSError("smtp down"))
for _ in range(3):
    nt._deliver("capt", contacts["capt"], "email", "s", "b")
assert "send:email" in health.problems()
print("notifier OK")

# ------------------------------------------------------------------- bot
d = tempfile.mkdtemp()
cfg = {"watchlist_file": os.path.join(d, "vessels.csv"), "contacts_file": os.path.join(d, "contacts.yaml"),
       "state_db": os.path.join(d, "state.db"), "timezone": "America/Los_Angeles"}
store.save_vessels(cfg["watchlist_file"], [])
store.save_contacts(cfg["contacts_file"], [
    {"id": "capt", "name": "Capt Cruz", "role": "port_captain", "telegram_chat_id": "111", "channels": ["telegram"], "can_manage_vessels": True},
    {"id": "agent", "name": "Agent Ames", "role": "boarding_agent", "telegram_chat_id": "222", "channels": ["telegram"], "can_manage_vessels": True},
    {"id": "viewer", "name": "Vera Viewer", "role": "port_captain", "telegram_chat_id": "333", "channels": ["telegram"]},
    {"id": "boss", "name": "The Boss", "role": "other", "telegram_chat_id": "444", "channels": ["telegram"], "can_manage_vessels": True},
])
api_calls = []
bot = Bot(cfg, health=Health({}), api=lambda m, p=None, timeout=15: api_calls.append((m, p)) or [])
say = bot.handle_text
vessels = lambda: store.load_vessels(cfg["watchlist_file"])

# strangers
assert "555" in say(555, "/start") and "People page" in say(555, "/start")
assert say(555, "/list") is None and say(555, "/add 366000001 X") is None and say(555, "hello") is None
assert "555" in say(555, "/id")
assert vessels() == []

# captain adds
r = say(111, "/add 366000001 Pacific Star")
assert r.startswith("Added Pacific Star (366000001)") and "get its alerts" in r, r
v = vessels()[0]
assert (v["mmsi"], v["name"], v["port_captains"], v["boarding_agents"]) == ("366000001", "Pacific Star", ["capt"], [])
assert "already on your list" in say(111, "/add 366000001")
# agent joins the same vessel instead of duplicating it
r = say(222, "/add 366000001")
assert "I added you" in r and len(vessels()) == 1 and vessels()[0]["boarding_agents"] == ["agent"], r
# name-only add, then the MMSI arrives and upgrades the same entry
r = say(222, "/add Ever Given")
assert "no MMSI" in r and len(vessels()) == 2, r
r = say(222, "/add 366000002 ever   given")
assert len(vessels()) == 2 and {x["mmsi"] for x in vessels()} == {"366000001", "366000002"}, vessels()
# bad input
assert "9 digits" in say(111, "/add 12345")
assert "Tell me which" in say(111, "/add")
v = None
say(111, "/add 366000003 =HYPERLINK(evil)")
assert not any(x["name"].startswith("=") for x in vessels()), vessels()
# viewer can read but not change
assert "permission" in say(333, "/add 366000009 X") and "permission" in say(333, "/remove 366000001")
assert "Pacific Star" in say(333, "/list") and "Capt Cruz" in say(333, "/list")
assert "No vessels assigned" in say(333, "/mine")
# mine / list
mine = say(111, "/mine")
assert "Pacific Star" in mine and "Ever Given" not in mine, mine
assert "★" in say(111, "/list")
# remove: only unassigns you; vessel stays while someone else has it
r = say(111, "/remove 366000001")
assert "Took you off" in r and "Agent Ames" in r and len(vessels()) == 3, r
assert "isn't assigned to you" in say(111, "/remove 366000001")
assert "isn't on the list" in say(111, "/remove 366999999")
# last person leaving deletes it
r = say(222, "/remove 366000001")
assert "off the list" in r and "366000001" not in [x["mmsi"] for x in vessels()], r
# "other" role: adds unassigned, deletes for everyone
r = say(444, "/add 366000050 Boss Boat")
assert "Nobody is assigned" in r
assert "web page" in say(444, "/help")
r = say(444, "/remove 366000002")
assert "for everyone" in r and "Agent Ames" in r and "366000002" not in [x["mmsi"] for x in vessels()], r
# status
s = say(111, "/status")
assert "Watching" in s and "AIS feed" in s, s
assert "don't know" in say(111, "/bogus")
assert "Send /help" in say(111, "hello there")
assert say(111, "/add@MyBot 366000077 Bot Suffix").startswith("Added")

# transport: group chats ignored, private chats answered, long replies split
api_calls.clear()
bot.process_update({"update_id": 1, "message": {"chat": {"id": 111, "type": "group"}, "text": "/list"}})
assert api_calls == []
bot.process_update({"update_id": 2, "message": {"chat": {"id": 111, "type": "private"}, "text": "/list"}})
assert api_calls and api_calls[0][0] == "sendMessage" and api_calls[0][1]["chat_id"] == 111
bot.process_update({"update_id": 3, "message": {"chat": {"id": 111, "type": "private"}, "sticker": {}}})
assert len(api_calls) == 1
big = "\n".join(f"line {i} " + "x" * 90 for i in range(200))
parts = chunks(big)
assert len(parts) > 1 and all(len(p) <= 3800 for p in parts) and "\n".join(parts) == big
assert chunks("") == [""] and chunks("a" * 9000, 3800)[0] == "a" * 3800
print("bot OK")
print("ALL OK")
