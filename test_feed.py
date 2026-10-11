"""End-to-end test of the feed loop, health monitor, heartbeat and live reload.

Uses a fake `websockets` module, so it needs no network and no extra packages.
Takes about 12 seconds because it runs in (sped up) real time.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import types

import store

# ---- fake websockets ---------------------------------------------------------
connections = []   # every FakeWS that was opened
script = []        # queues handed out, one per connect()


class FakeWS:
    def __init__(self, q):
        self.q, self.sent = q, []

    async def send(self, s):
        self.sent.append(s)

    async def close(self):
        self.q.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.q.get()
        if item is None:
            raise StopAsyncIteration
        return item


class FakeConnect:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        connections.append(self.ws)
        return self.ws

    async def __aexit__(self, *a):
        return False


fake = types.ModuleType("websockets")
fake.connect = lambda *a, **k: FakeConnect(FakeWS(script.pop(0)))
sys.modules["websockets"] = fake

import main  # noqa: E402  (must come after the fake is installed)
from health import Health  # noqa: E402
from notifier import Notifier  # noqa: E402
from settings import expand_env, load_yaml  # noqa: E402
from tracker import Tracker, Watchlist, apply_zones  # noqa: E402


def pos(lat, lon, name="EXAMPLE VESSEL ONE", mmsi=123456789, cog=0.0, sog=12.0):
    return json.dumps({
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": mmsi, "ShipName": name + " ", "latitude": lat, "longitude": lon},
        "Message": {"PositionReport": {"Sog": sog, "Cog": cog, "TrueHeading": 511}},
    })


async def scenario():
    d = tempfile.mkdtemp()
    for f in ("vessels.csv", "contacts.yaml", "zones.yaml"):
        shutil.copy(os.path.join("defaults", f), os.path.join(d, f))
    with open(os.path.join(d, "vessels.csv"), "a") as f:
        f.write("123456789,,EXAMPLE VESSEL ONE,capt,agent\n")
    store.save_contacts(os.path.join(d, "contacts.yaml"), [
        {"id": "capt", "name": "Capt", "role": "port_captain", "email": "c@x.com", "channels": ["email"]},
        {"id": "agent", "name": "Agent", "role": "boarding_agent", "email": "a@x.com", "channels": ["email"]},
        {"id": "admin", "name": "Admin", "role": "other", "email": "admin@x.com", "channels": ["email"],
         "health_alerts": True},
    ])
    cfg = expand_env(load_yaml("config.yaml"))
    cfg.update({
        "watchlist_file": os.path.join(d, "vessels.csv"), "contacts_file": os.path.join(d, "contacts.yaml"),
        "zones_file": os.path.join(d, "zones.yaml"), "state_db": os.path.join(d, "state.db"),
        "health_file": os.path.join(d, "health.json"),
        "health_interval_s": 0.2, "feed_silence_minutes": 0.05, "disconnect_alert_minutes": 0.03,
        "probe_minutes": 0.05, "reload_interval_s": 0.3,
        "heartbeat": {"url": "http://watchdog.test/ping", "interval_minutes": 0.01},
    })
    apply_zones(cfg)
    contacts = load_yaml(cfg["contacts_file"])
    watch = Watchlist(cfg["watchlist_file"])
    tracker = Tracker(cfg, watch, cfg["state_db"])
    nt = Notifier(cfg, contacts)
    health = Health(cfg)
    nt.on_result = health.on_send_result

    log = []   # (time, recipient, subject)
    nt._email = lambda c, s, b: log.append((time.time(), c["email"], s))
    nt._probe_email = lambda: None
    pings = []
    main.ping = lambda url: pings.append(time.time()) or True

    subjects = lambda to=None: [s for _, r, s in log if to in (None, r)]
    pilot = cfg["zones"]["pilot_station"]["center"]

    q1, q2, q3 = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
    q1.put_nowait(json.dumps({"error": "Api Key Is Not Valid"}))
    q1.put_nowait(None)                       # server hangs up after the error
    script.extend([q1, q2, q3])

    runner = asyncio.create_task(main.run(cfg, tracker, nt, watch, health))

    # 1. startup notice, then the bad-key error is reported
    await asyncio.sleep(0.8)
    assert any("Tracker started" in s for s in subjects("admin@x.com")), log
    assert any("PROBLEM: AIS feed error" in s for s in subjects("admin@x.com")), log
    assert not pings, "no heartbeat while unhealthy"

    # 2. second connection works; an inbound ship reaches the pilot station
    await asyncio.sleep(1.9)
    assert len(connections) == 2, len(connections)
    q2.put_nowait(pos(pilot[0] - 0.2, pilot[1]))
    q2.put_nowait(pos(pilot[0], pilot[1]))
    await asyncio.sleep(0.6)
    assert any("reached LA/LB Pilot Station" in s for s in subjects("c@x.com")), log
    assert any("reached LA/LB Pilot Station" in s for s in subjects("a@x.com")), log
    assert not any("reached" in s for s in subjects("admin@x.com")), "admin only gets system messages"
    assert any("Recovered: AIS feed error" in s for s in subjects("admin@x.com")), log
    status = json.load(open(cfg["health_file"]))
    assert status["ok"] and status["alerts_sent"] == 1 and status["vessels"] == 1, status
    t_ok = time.time()

    # 3. the feed goes quiet: silence is reported and the heartbeat stops
    await asyncio.sleep(3.8)
    assert any("PROBLEM: AIS feed silent" in s for s in subjects("admin@x.com")), log
    t_silent = next(t for t, r, s in log if "PROBLEM: AIS feed silent" in s)
    assert any(t_ok < p < t_silent for p in pings), "heartbeat should run while healthy"
    assert not json.load(open(cfg["health_file"]))["ok"]

    # 4. data flows again: recovery notice, heartbeat resumes
    q2.put_nowait(pos(33.5, -118.5, "SOMEONE ELSE", 555000111))
    await asyncio.sleep(0.5)
    t_rec = next(t for t, r, s in log if "Recovered: AIS feed silent" in s)
    assert not any(t_silent + 0.05 < p < t_rec - 0.05 for p in pings), "no heartbeat while the problem was active"
    await asyncio.sleep(1.0)
    assert any(p > t_rec for p in pings), "heartbeat resumes"

    # 5. editing the zones file (area box) reconnects with the new box
    zdata = store.load_zones(cfg["zones_file"])
    zdata["bounding_box"][0][0] = 33.30
    store.save_zones(cfg["zones_file"], zdata)
    await asyncio.sleep(2.2)
    assert len(connections) == 3, len(connections)
    assert json.loads(connections[2].sent[0])["BoundingBoxes"][0][0][0] == 33.30
    assert "FiltersShipMMSI" not in json.loads(connections[2].sent[0]), "default is to filter locally"
    assert health.reconnects == 3

    # 6. a malformed report does not drop the connection
    q3.put_nowait("not json")
    q3.put_nowait(json.dumps({"MessageType": "PositionReport", "MetaData": {}, "Message": {}}))
    q3.put_nowait(pos(pilot[0] - 0.2, pilot[1]))
    await asyncio.sleep(0.5)
    assert len(connections) == 3 and health.connected

    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass
    print(f"admin received {len(subjects('admin@x.com'))} system messages, {len(pings)} heartbeats")


asyncio.run(scenario())
print("FEED OK")
