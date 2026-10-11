"""Two-way Telegram bot for the people listed on the People page.

Uses long polling, so nothing has to be reachable from the internet. Only
private chats from a registered Telegram chat id get answers (strangers get
a reply to /start, /help and /id only).
"""
import asyncio
import json
import os
import urllib.error
import urllib.request
from datetime import datetime

import store

MAX_VESSELS = 500
MAX_REPLY = 3800

COMMANDS = [
    {"command": "mine", "description": "Vessels assigned to me"},
    {"command": "list", "description": "Every vessel being watched"},
    {"command": "add", "description": "Watch a vessel: /add MMSI NAME"},
    {"command": "remove", "description": "Stop watching: /remove MMSI"},
    {"command": "status", "description": "Is the tracker healthy?"},
    {"command": "help", "description": "What I can do"},
]


def norm(s):
    return " ".join((s or "").upper().split())


def make_api(token):
    """Return call(method, params, timeout). Errors never include the token."""

    def call(method, params=None, timeout=15):
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=json.dumps(params or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"telegram {method}: HTTP {e.code}") from None
        except Exception as e:
            raise RuntimeError(f"telegram {method}: {type(e).__name__}") from None
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data["result"]

    return call


def chunks(text, size=MAX_REPLY):
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if cur and len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


def ago(sec):
    sec = int(sec)
    if sec < 90:
        return f"{sec}s ago"
    if sec < 5400:
        return f"{sec // 60} min ago"
    if sec < 172800:
        return f"{sec // 3600} h ago"
    return f"{sec // 86400} days ago"


class Bot:
    def __init__(self, cfg, health=None, api=None):
        self.vessels_path = cfg.get("watchlist_file", "data/vessels.csv")
        self.contacts_path = cfg.get("contacts_file", "data/contacts.yaml")
        self.tz = cfg.get("timezone") or "America/Los_Angeles"
        self.health = health
        self.api = api or make_api(cfg["telegram"]["bot_token"])
        state_dir = os.path.dirname(cfg.get("state_db") or "data/state.db") or "."
        self.offset_path = os.path.join(state_dir, "telegram_offset")

    # ---- people and vessels -------------------------------------------------
    def _who(self, chat_id):
        for c in store.load_contacts(self.contacts_path):
            if str(c.get("telegram_chat_id", "")).strip() == str(chat_id):
                return c
        return None

    @staticmethod
    def _slot(who):
        return {"port_captain": "port_captains", "boarding_agent": "boarding_agents"}.get(who.get("role"))

    @staticmethod
    def _fmt(v):
        if v["mmsi"]:
            return f"{v['name'] or 'unnamed'} ({v['mmsi']})"
        return v["name"]

    @staticmethod
    def _find(vessels, mmsi, name):
        if mmsi:
            for v in vessels:
                if v["mmsi"] == mmsi:
                    return v
            if name:  # a name-only entry for the same ship: fill in its MMSI instead of duplicating
                for v in vessels:
                    if not v["mmsi"] and norm(v["name"]) == norm(name):
                        return v
            return None
        if name:
            for v in vessels:
                if norm(v["name"]) == norm(name):
                    return v
        return None

    @staticmethod
    def _parse(args):
        parts = args.split()
        mmsi = ""
        if parts and parts[0].isascii() and parts[0].isdigit():
            if len(parts[0]) != 9:
                return "", "", "An MMSI is exactly 9 digits. Check the number and try again."
            mmsi, parts = parts[0], parts[1:]
        return mmsi, store.clean_name(" ".join(parts)), None

    def _names(self, ids):
        by_id = {c["id"]: c["name"] for c in store.load_contacts(self.contacts_path)}
        return ", ".join(by_id.get(i, i) for i in ids) or "nobody"

    # ---- message handling ---------------------------------------------------
    def handle_text(self, chat_id, text):
        """Return the reply text, or None to stay quiet."""
        text = (text or "").strip()
        if not text:
            return None
        who = self._who(chat_id)
        if not text.startswith("/"):
            return "Send /help to see what I can do." if who else None
        cmd, _, args = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        args = args.strip()
        if cmd == "/id":
            return f"This chat's id is {chat_id}."
        if not who:
            if cmd in ("/start", "/help"):
                return (
                    f"Hi. I don't know this chat yet. Your chat id is {chat_id}. "
                    "Send it to whoever runs the vessel alerts and they can add you on the People page."
                )
            return None
        fn = {
            "/start": self.c_help, "/help": self.c_help, "/mine": self.c_mine, "/list": self.c_list,
            "/status": self.c_status, "/add": self.c_add, "/remove": self.c_remove,
        }.get(cmd)
        if not fn:
            return "I don't know that one. Send /help."
        try:
            return fn(who, args)
        except Exception as e:
            print(f"[error] telegram command {cmd} failed: {type(e).__name__}: {e}")
            return "Something went wrong on my end. Try again, and tell whoever runs this if it keeps happening."

    def c_help(self, who, args):
        lines = [f"Hi {who['name']}.", "", "/mine - vessels assigned to you", "/list - every vessel being watched",
                 "/status - is the tracker healthy", "/id - show this chat's id"]
        if who.get("can_manage_vessels"):
            if self._slot(who):
                lines += ["", "/add 366123456 PACIFIC STAR - watch a vessel and get its alerts",
                          "/add PACIFIC STAR - same, by name only (less reliable than the MMSI)",
                          "/remove 366123456 - stop getting alerts for it (it comes off the list when nobody is left)"]
            else:
                lines += ["", "/add 366123456 PACIFIC STAR - add a vessel (nobody is assigned, do that on the web page)",
                          "/add PACIFIC STAR - same, by name only (less reliable than the MMSI)",
                          "/remove 366123456 - take a vessel off the list for everyone"]
        else:
            lines += ["", "You can look but not change the list. Ask whoever runs this if you need that."]
        return "\n".join(lines)

    def c_mine(self, who, args):
        slot = self._slot(who)
        if not slot:
            return "You're not set up as a port captain or boarding agent, so no vessels are assigned to you. /list shows everything."
        mine = [v for v in store.load_vessels(self.vessels_path) if who["id"] in v[slot]]
        if not mine:
            return "No vessels assigned to you yet." + (" Add one with /add." if who.get("can_manage_vessels") else "")
        mine.sort(key=lambda v: norm(v["name"]) or v["mmsi"])
        return f"Your vessels ({len(mine)}):\n" + "\n".join("• " + self._fmt(v) for v in mine)

    def c_list(self, who, args):
        vessels = store.load_vessels(self.vessels_path)
        if not vessels:
            return "Nothing is being watched yet."
        by_id = {c["id"]: c["name"] for c in store.load_contacts(self.contacts_path)}
        slot = self._slot(who)
        lines = []
        for v in sorted(vessels, key=lambda v: norm(v["name"]) or v["mmsi"]):
            who_bits = [by_id.get(i, i) for i in v["port_captains"] + v["boarding_agents"]]
            star = "★ " if slot and who["id"] in v[slot] else "• "
            lines.append(star + self._fmt(v) + (f" - {', '.join(who_bits)}" if who_bits else " - nobody assigned"))
        return f"Watching {len(vessels)} vessel(s):\n" + "\n".join(lines)

    def c_status(self, who, args):
        n = len(store.load_vessels(self.vessels_path))
        if not self.health:
            return f"Watching {n} vessel(s)."
        s = self.health.snapshot(vessels=n)
        lines = ["Everything looks fine." if s["ok"] else "PROBLEM: " + "; ".join(s["problems"])]
        if s["connected"]:
            age = s["last_message_age_s"]
            lines.append("AIS feed: connected" + (f", last position report {ago(age)}" if age is not None else ", waiting for the first report"))
        else:
            lines.append("AIS feed: not connected")
        lines.append(f"Watching {n} vessel(s). Alerts sent since the tracker started: {s['alerts_sent']}.")
        la = s["last_alert"]
        if la:
            try:
                from zoneinfo import ZoneInfo
                when = datetime.fromtimestamp(la["time"], ZoneInfo(self.tz)).strftime("%a %b %d %H:%M")
            except Exception:
                when = ago(self.health.clock() - la["time"])
            lines.append(f"Last alert: {la['vessel']} at {la['zone']}, {when}.")
        return "\n".join(lines)

    def c_add(self, who, args):
        if not who.get("can_manage_vessels"):
            return "You don't have permission to change the vessel list. Ask whoever runs the alerts."
        mmsi, name, err = self._parse(args)
        if err:
            return err
        if not mmsi and not name:
            return "Tell me which vessel: /add 366123456 PACIFIC STAR (or just /add PACIFIC STAR)."
        slot = self._slot(who)
        with store.lock(self.vessels_path):
            vessels = store.load_vessels(self.vessels_path)
            v = self._find(vessels, mmsi, name)
            is_new = v is None
            if is_new:
                if len(vessels) >= MAX_VESSELS:
                    return f"The list is full ({MAX_VESSELS} vessels). Remove some first."
                v = {"mmsi": mmsi, "imo": "", "name": name, "port_captains": [], "boarding_agents": []}
                vessels.append(v)
            changed = is_new
            if mmsi and not v["mmsi"]:
                v["mmsi"], changed = mmsi, True
            if name and not v["name"]:
                v["name"], changed = name, True
            joined = bool(slot) and who["id"] not in v[slot]
            if joined:
                v[slot].append(who["id"])
                changed = True
            if changed:
                store.save_vessels(self.vessels_path, vessels)
        label = self._fmt(v)
        if is_new:
            msg = f"Added {label}."
            msg += " You'll get its alerts." if slot else " Nobody is assigned to it yet, so only people set to get every alert will hear about it. Assign people on the web page."
            if not v["mmsi"]:
                msg += " There's no MMSI, so I'll match on the name the ship broadcasts, which is less reliable. /add it again with the MMSI when you have it."
            return msg + " It takes effect within a few seconds."
        if joined:
            return f"{label} was already on the list. I added you to it."
        return f"{label} is already on {'your' if slot else 'the'} list."

    def c_remove(self, who, args):
        if not who.get("can_manage_vessels"):
            return "You don't have permission to change the vessel list. Ask whoever runs the alerts."
        mmsi, name, err = self._parse(args)
        if err:
            return err
        if not mmsi and not name:
            return "Tell me which vessel: /remove 366123456 (or the name)."
        slot = self._slot(who)
        with store.lock(self.vessels_path):
            vessels = store.load_vessels(self.vessels_path)
            v = self._find(vessels, mmsi, name)
            if v is None:
                return "That vessel isn't on the list. /mine shows yours."
            label = self._fmt(v)
            if slot is None:
                assigned = self._names(v["port_captains"] + v["boarding_agents"])
                vessels.remove(v)
                store.save_vessels(self.vessels_path, vessels)
                return f"Removed {label} for everyone (it was assigned to: {assigned}). /add puts it back, but not the assignments."
            if who["id"] not in v[slot]:
                return f"{label} isn't assigned to you."
            v[slot].remove(who["id"])
            if not v["port_captains"] and not v["boarding_agents"]:
                vessels.remove(v)
                store.save_vessels(self.vessels_path, vessels)
                return f"Stopped watching {label}. Nobody else was assigned, so it's off the list."
            store.save_vessels(self.vessels_path, vessels)
            rest = self._names(v["port_captains"] + v["boarding_agents"])
            return f"Took you off {label}. Still watched for: {rest}."

    # ---- transport ----------------------------------------------------------
    def process_update(self, update):
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private" or "id" not in chat:
            return
        try:
            reply = self.handle_text(chat["id"], msg.get("text"))
            if reply:
                for part in chunks(reply):
                    self.api("sendMessage", {"chat_id": chat["id"], "text": part})
        except Exception as e:
            print(f"[warn] telegram update failed: {e}")

    def _load_offset(self):
        try:
            with open(self.offset_path, encoding="utf-8") as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return 0

    def _save_offset(self, offset):
        try:
            with open(self.offset_path, "w", encoding="utf-8") as f:
                f.write(str(offset))
        except OSError:
            pass

    async def run(self):
        try:
            await asyncio.to_thread(self.api, "setMyCommands", {"commands": COMMANDS})
        except Exception as e:
            print(f"[warn] could not set the Telegram command menu: {e}")
        offset = self._load_offset()
        backoff = 2
        print("[info] Telegram bot listening")
        while True:
            try:
                updates = await asyncio.to_thread(
                    self.api, "getUpdates",
                    {"timeout": 30, "offset": offset, "allowed_updates": ["message"]}, 45,
                )
                backoff = 2
                for u in updates:
                    offset = max(offset, u["update_id"] + 1)
                    await asyncio.to_thread(self.process_update, u)
                if updates:
                    self._save_offset(offset)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[warn] telegram poll failed: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
