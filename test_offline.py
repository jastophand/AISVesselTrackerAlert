"""Offline test: fake position tracks through the tracker, no network needed."""
import os
import tempfile

from notifier import Notifier
from settings import expand_env, load_yaml
from tracker import Tracker, Watchlist, apply_zones

tmp = tempfile.mkdtemp()
VESSELS = os.path.join(tmp, "vessels.csv")
with open(VESSELS, "w") as f:
    f.write(
        "mmsi,imo,name,port_captain,boarding_agent\n"
        "123456789,9000001,EXAMPLE VESSEL ONE,capt_example,agent_example\n"
        "987654321,9000002,EXAMPLE VESSEL TWO,capt_example;jason,agent_example\n"
    )
contacts = {
    "jason": {"name": "Jason", "role": "other", "email": "j@example.com", "channels": ["email"]},
    "capt_example": {"name": "Capt", "role": "port_captain", "email": "c@example.com",
                     "ntfy_topic": "t", "channels": ["email", "ntfy"]},
    "agent_example": {"name": "Agent", "role": "boarding_agent", "email": "a@example.com",
                      "telegram_chat_id": "1", "channels": ["email", "telegram"]},
    "ops": {"name": "Ops desk", "role": "other", "email": "ops@example.com",
            "channels": ["email"], "always_notify": True},
}

cfg = expand_env(load_yaml("config.yaml"))
cfg["zones_file"] = "defaults/zones.yaml"
cfg["dry_run"] = True
apply_zones(cfg)
watch = Watchlist(VESSELS)
nt = Notifier(cfg, contacts)
pilot = cfg["zones"]["pilot_station"]["center"]


def fresh():
    return Tracker(cfg, watch, os.path.join(tempfile.mkdtemp(), "t.db"))


def run(tr, mmsi, name, track):
    now, out = 1_000_000.0, []
    for label, lat, lon, sog, cog in track:
        now += 3600
        evs = tr.process(mmsi, name, lat, lon, sog=sog, cog=cog, now=now)
        print(f"  {label:20} -> {[e['zone'] for e in evs]}")
        out += evs
        for e in evs:
            nt.send(e)
    return out


print("INBOUND (heading north toward the harbor):")
inbound = run(fresh(), "123456789", "EXAMPLE VESSEL ONE", [
    ("far out", pilot[0] - 0.2, pilot[1], 12, 0),
    ("approach", pilot[0] - 0.05, pilot[1], 12, 0),
    ("at pilot station", pilot[0], pilot[1], 11, 0),       # alert
    ("still there", pilot[0] + 0.001, pilot[1], 11, 0),    # no repeat
    ("inside port", 33.71, -118.2, 8, 20),                 # alert
])
assert [e["zone"] for e in inbound] == ["pilot_station", "port_limits"], inbound
who = [i for i, _ in nt.recipients(inbound[0])]
assert who == ["capt_example", "agent_example", "ops"], who  # assigned people + every-alert contact

print("OUTBOUND (leaving the harbor, heading south):")
outbound = run(fresh(), "987654321", "EXAMPLE VESSEL TWO", [
    ("inside port", 33.71, -118.2, 8, 200),
    ("leaving", pilot[0] + 0.05, pilot[1], 12, 180),
    ("at pilot station", pilot[0], pilot[1], 12, 180),     # no alert
    ("past it", pilot[0] - 0.2, pilot[1], 14, 180),
    ("back toward port", pilot[0] - 0.02, pilot[1], 12, 0),  # re-entering inbound -> alert
])
assert [e["zone"] for e in outbound] == ["pilot_station"], outbound

print("DRIFTING (stopped, course unreliable) then moves inbound:")
drift = run(fresh(), "123456789", "EXAMPLE VESSEL ONE", [
    ("outside", pilot[0] - 0.2, pilot[1], 10, 0),
    ("drifting in zone", pilot[0], pilot[1], 0.3, 180),    # no alert, unknown
    ("underway inbound", pilot[0] + 0.002, pilot[1], 6, 10),  # alert
])
assert len(drift) == 1 and drift[0]["zone"] == "pilot_station", drift

assert fresh().process("555", "NOT WATCHED", pilot[0], pilot[1], sog=10, cog=0) == []
print("OK")
