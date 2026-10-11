"""Notification delivery: email (SMTP), ntfy push, Telegram. Stdlib only."""
import json
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage


def format_event(ev, tz_name=None):
    t = ev["time_utc"]
    try:
        if tz_name:
            from zoneinfo import ZoneInfo
            t = t.astimezone(ZoneInfo(tz_name))
    except Exception:
        pass
    when = t.strftime("%a %b %d %H:%M %Z")
    speed = f", {ev['sog']:.1f} kn" if ev.get("sog") is not None else ""
    subject = f"{ev['vessel']} reached {ev['zone_label']}"
    body = (
        f"{ev['vessel']} has reached {ev['zone_label']}.\n\n"
        f"Time: {when}\n"
        f"Position: {ev['lat']:.4f}, {ev['lon']:.4f}{speed}\n"
        f"MMSI: {ev['mmsi']}" + (f"   IMO: {ev['imo']}" if ev["imo"] else "") + "\n"
        f"Map: https://www.google.com/maps?q={ev['lat']:.5f},{ev['lon']:.5f}\n"
    )
    return subject, body


class Notifier:
    def __init__(self, cfg, contacts):
        self.cfg = cfg
        self.contacts = contacts
        self.tz = cfg.get("timezone", "America/Los_Angeles")
        self.dry_run = cfg.get("dry_run", False)
        self.on_result = None  # optional callback(channel, ok, error_text)

    def recipients(self, ev):
        ids = []
        always = [cid for cid, c in self.contacts.items() if (c or {}).get("always_notify")]
        for i in ev["port_captains"] + ev["boarding_agents"] + self.cfg.get("always_notify", []) + always:
            if i not in ids:
                ids.append(i)
        out = []
        for i in ids:
            c = self.contacts.get(i)
            if c:
                out.append((i, c))
            else:
                print(f"[warn] contact '{i}' not found in contacts.yaml")
        return out

    def send(self, ev):
        subject, body = format_event(ev, self.tz)
        for cid, c in self.recipients(ev):
            for ch in c.get("channels", ["email"]):
                self._deliver(cid, c, ch, subject, body)

    def send_admin(self, subject, body, exclude=()):
        """System messages (health problems) to everyone marked for health alerts."""
        sent = 0
        for cid, c in self.contacts.items():
            c = c or {}
            if not c.get("health_alerts"):
                continue
            for ch in c.get("channels", ["email"]):
                if ch in exclude:
                    continue
                if self._deliver(cid, c, ch, f"[Vessel alerts] {subject}", body):
                    sent += 1
        if sent == 0:
            print(f"[warn] nobody could be told: {subject}")
        return sent

    def probe_all(self):
        """Check each channel in use is reachable, so a dead one is found before an alert needs it."""
        used = set()
        for c in self.contacts.values():
            used.update((c or {}).get("channels", ["email"]))
        for ch in sorted(used):
            fn = getattr(self, f"_probe_{ch}", None)
            if fn is None:
                continue
            try:
                fn()
                ok, err = True, None
            except Exception as e:
                ok, err = False, f"{type(e).__name__}: {e}"
                print(f"[warn] {ch} check failed: {err}")
            if self.on_result:
                self.on_result(ch, ok, err)

    def _deliver(self, cid, c, ch, subject, body):
        if self.dry_run:
            print(f"[dry-run] -> {cid} via {ch}: {subject}")
            return True
        try:
            getattr(self, f"_{ch}")(c, subject, body)
            print(f"[sent] {cid} via {ch}: {subject}")
            ok, err = True, None
        except Exception as e:  # keep going if one channel fails
            ok, err = False, f"{type(e).__name__}: {e}"
            print(f"[error] {cid} via {ch}: {err}")
        if self.on_result:
            try:
                self.on_result(ch, ok, err)
            except Exception:
                pass
        return ok

    # --- email ---
    def _smtp_open(self):
        s = self.cfg.get("smtp") or {}
        if not s.get("host"):
            raise RuntimeError("SMTP host is not set")
        security = (s.get("security") or "starttls").lower()
        port = int(s.get("port") or 587)
        if security == "ssl":
            srv = smtplib.SMTP_SSL(s["host"], port, timeout=20)
        else:
            srv = smtplib.SMTP(s["host"], port, timeout=20)
        try:
            if security == "starttls":
                srv.starttls()
            if s.get("username"):
                srv.login(s["username"], s["password"])
        except Exception:
            srv.close()
            raise
        return srv

    def _email(self, c, subject, body):
        s = self.cfg.get("smtp") or {}
        sender = s.get("from") or s.get("username")
        if not sender:
            raise RuntimeError("SMTP from address is not set")
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = c["email"]
        msg.set_content(body)
        with self._smtp_open() as srv:
            srv.send_message(msg)

    def _probe_email(self):
        with self._smtp_open() as srv:
            srv.noop()

    # --- ntfy ---
    def _ntfy_server(self):
        n = self.cfg.get("ntfy") or {}
        return (n.get("server") or "https://ntfy.sh").rstrip("/")

    def _ntfy(self, c, subject, body):
        n = self.cfg.get("ntfy") or {}
        req = urllib.request.Request(
            f"{self._ntfy_server()}/{c['ntfy_topic']}",
            data=body.encode(),
            headers={"Title": subject.encode("ascii", "replace").decode(), "Priority": "high"},
        )
        if n.get("token"):
            req.add_header("Authorization", f"Bearer {n['token']}")
        urllib.request.urlopen(req, timeout=15).read()

    def _probe_ntfy(self):
        try:
            urllib.request.urlopen(urllib.request.Request(self._ntfy_server() + "/v1/health"), timeout=15).read()
        except urllib.error.HTTPError as e:
            if e.code >= 500:  # any answer below 500 means the server is up
                raise

    # --- telegram ---
    def _telegram_token(self):
        token = (self.cfg.get("telegram") or {}).get("bot_token")
        if not token:
            raise RuntimeError("Telegram bot token is not set")
        return token

    def _telegram(self, c, subject, body):
        data = json.dumps({"chat_id": c["telegram_chat_id"], "text": f"{subject}\n\n{body}"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self._telegram_token()}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15).read()

    def _probe_telegram(self):
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{self._telegram_token()}/getMe"), timeout=15
        ).read()
