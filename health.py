"""Health tracking for the tracker process.

Watches the AIS feed and notification delivery, and writes a small status file
that the web UI and the Docker healthcheck read.
"""
import json
import os
import time
import urllib.request


class Health:
    def __init__(self, cfg, clock=time.time):
        self.clock = clock
        self.started = clock()
        self.silence_s = float(cfg.get("feed_silence_minutes") or 10) * 60
        self.disconnect_s = float(cfg.get("disconnect_alert_minutes") or 5) * 60
        self.send_fail_n = int(cfg.get("send_failures_before_alert") or 3)
        self.connected = False
        self.down_since = self.started  # counts as "down" until the first connect
        self.up_since = None
        self.last_msg = None
        self.messages = 0
        self.reconnects = 0
        self.feed_error = None
        self.alerts_sent = 0
        self.last_alert = None
        self.fail_counts = {}
        self.last_fail = {}

    # ---- events from the feed -------------------------------------------
    def on_connect(self):
        self.connected = True
        self.up_since = self.clock()
        self.down_since = None
        self.reconnects += 1

    def on_disconnect(self):
        if self.connected or self.down_since is None:
            self.connected = False
            self.down_since = self.clock()

    def on_message(self):
        self.last_msg = self.clock()
        self.messages += 1
        self.feed_error = None  # real data is flowing again

    def on_feed_error(self, text):
        self.feed_error = str(text)[:200]

    def on_alert(self, ev):
        self.alerts_sent += 1
        self.last_alert = {"vessel": ev.get("vessel"), "zone": ev.get("zone_label"), "time": self.clock()}

    # ---- events from notification delivery --------------------------------
    def on_send_result(self, channel, ok, err=None):
        if ok:
            self.fail_counts[channel] = 0
        else:
            self.fail_counts[channel] = self.fail_counts.get(channel, 0) + 1
            self.last_fail[channel] = str(err)[:200]

    # ---- evaluation -------------------------------------------------------
    def problems(self):
        """Return {key: (title, detail)} for everything that is wrong right now."""
        now = self.clock()
        out = {}
        if self.feed_error:
            out["feed_error"] = ("AIS feed error", f"AISStream said: {self.feed_error}")
        if not self.connected:
            if self.down_since is not None and now - self.down_since >= self.disconnect_s:
                mins = int((now - self.down_since) / 60)
                out["disconnected"] = (
                    "AIS feed disconnected",
                    f"Not connected to AISStream for {mins} min, so no vessel alerts can fire.",
                )
        else:
            ref = self.last_msg if (self.last_msg and self.last_msg >= self.up_since) else self.up_since
            if now - ref >= self.silence_s:
                mins = int((now - ref) / 60)
                out["silent"] = (
                    "AIS feed silent",
                    f"Connected to AISStream but no position reports for {mins} min. "
                    "The feed may be stalled, or the area box is quiet "
                    "(raise FEED_SILENCE_MINUTES if that is normal).",
                )
        for ch, n in self.fail_counts.items():
            if n >= self.send_fail_n:
                out[f"send:{ch}"] = (
                    f"{ch} delivery failing",
                    f"{n} {ch} checks or sends in a row have failed. Last error: {self.last_fail.get(ch, '?')}",
                )
        return out

    def snapshot(self, problems=None, vessels=0):
        now = self.clock()
        problems = self.problems() if problems is None else problems
        return {
            "updated_at": now,
            "started_at": self.started,
            "ok": not problems,
            "problems": [f"{t}: {d}" for t, d in problems.values()],
            "connected": self.connected,
            "last_message_age_s": None if self.last_msg is None else round(now - self.last_msg, 1),
            "messages": self.messages,
            "reconnects": self.reconnects,
            "vessels": vessels,
            "alerts_sent": self.alerts_sent,
            "last_alert": self.last_alert,
            "delivery_failures": {k: v for k, v in self.fail_counts.items() if v},
        }

    def write_status(self, path, snap):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f)
            os.replace(tmp, path)
        except OSError as e:
            print(f"[warn] could not write status file: {e}")


def ping(url, timeout=10):
    """Tell an external watchdog (Healthchecks.io, Uptime Kuma...) we are alive."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vessel-alerts"})
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception as e:  # never print the URL, it usually contains a secret
        print(f"[warn] heartbeat ping failed: {type(e).__name__}")
        return False
