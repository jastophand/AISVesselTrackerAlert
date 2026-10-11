"""Tests the web UI's logic without FastAPI installed.

A tiny stand-in for fastapi lets us import web.py and call its route functions
directly. It checks validation, saving and rendering, not HTTP or auth wiring.
"""
import os
import shutil
import sys
import tempfile
import time
import types

import yaml

import store

# ---- stand-in for fastapi ------------------------------------------------------
class _App:
    def __init__(self, **kw):
        self.routes = {}

    def get(self, path, **kw):
        def deco(fn):
            self.routes[path] = fn
            return fn
        return deco

    post = get


class HTTPException(Exception):
    def __init__(self, status_code, detail=None, headers=None):
        super().__init__(status_code, detail)
        self.status_code = status_code


class _R:
    def __init__(self, content="", status_code=200, media_type=None, headers=None):
        self.body, self.status_code, self.headers = content, status_code, headers or {}


class HTMLResponse(_R):
    pass


class Response(_R):
    pass


class RedirectResponse(_R):
    def __init__(self, url, status_code=307):
        super().__init__("", status_code)
        self.url = url


fa = types.ModuleType("fastapi")
fa.FastAPI, fa.Depends, fa.HTTPException, fa.Request = _App, (lambda f=None: None), HTTPException, object
fa.Form = lambda default=None, **kw: default
fr = types.ModuleType("fastapi.responses")
fr.HTMLResponse, fr.RedirectResponse, fr.Response = HTMLResponse, RedirectResponse, Response
fs = types.ModuleType("fastapi.security")
fs.HTTPBasic, fs.HTTPBasicCredentials = (lambda: None), object
sys.modules.update({"fastapi": fa, "fastapi.responses": fr, "fastapi.security": fs})

# ---- temp config and data ------------------------------------------------------
d = tempfile.mkdtemp()
for f in ("vessels.csv", "contacts.yaml", "zones.yaml"):
    shutil.copy(os.path.join("defaults", f), os.path.join(d, f))
cfgpath = os.path.join(d, "config.yaml")
yaml.safe_dump({
    "watchlist_file": os.path.join(d, "vessels.csv"), "contacts_file": os.path.join(d, "contacts.yaml"),
    "zones_file": os.path.join(d, "zones.yaml"), "health_file": os.path.join(d, "health.json"),
    "timezone": "${TIMEZONE:-America/Los_Angeles}",
}, open(cfgpath, "w"))
os.environ.update(CONFIG=cfgpath, ADMIN_PASSWORD="pw")

import web  # noqa: E402

V, C, Z = web.VESSELS, web.CONTACTS, web.ZONES
assert web.CFG["timezone"] == "America/Los_Angeles", "env defaults must be expanded in the web UI too"


def people(**kw):
    base = dict(is_edit="", id="", name="", role="other", email="", ntfy_topic="", telegram_chat_id="",
                channels=[], always_notify="", health_alerts="", can_manage_vessels="")
    base.update(kw)
    return web.contact_save(**base)


def err_of(r):
    assert r.status_code == 400, (r.status_code, r.body[:200])
    return r.body


# ---- auth ------------------------------------------------------------------------
creds = lambda u, p: types.SimpleNamespace(username=u, password=p)
assert web.auth(creds("admin", "pw")) is None
for bad in (creds("admin", "nope"), creds("root", "pw")):
    try:
        web.auth(bad)
        raise SystemExit("bad login accepted")
    except HTTPException as e:
        assert e.status_code == 401

# ---- people ----------------------------------------------------------------------
assert "No one yet" in web.contacts_page().body
assert "Id must be" in err_of(people(id="A", name="x", channels=["email"], email="a@b.c"))
assert "Name is required" in err_of(people(id="capt", channels=["email"], email="a@b.c"))
assert "email address" in err_of(people(id="capt", name="C", channels=["email"]))
assert "just digits" in err_of(people(id="capt", name="C", channels=["telegram"], telegram_chat_id="abc"))
assert "needs their Telegram chat id" in err_of(people(id="capt", name="C", channels=["email"], email="a@b.c", can_manage_vessels="1"))
assert people(id="capt", name="Capt Cruz", role="port_captain", email="c@x.com", channels=["email"]).status_code == 303
assert people(id="agent", name="Agent Ames", role="boarding_agent", email="a@x.com", telegram_chat_id="123456789",
              channels=["email", "telegram"], health_alerts="1", can_manage_vessels="1").status_code == 303
assert "already taken" in err_of(people(id="capt", name="Dup", channels=["email"], email="a@b.c"))
saved = {c["id"]: c for c in store.load_contacts(C)}
assert saved["agent"]["health_alerts"] is True and saved["agent"]["can_manage_vessels"] is True
assert "health_alerts" not in saved["capt"]
page = web.contacts_page().body
assert "manages vessels" in page and "health alerts" in page
form = web.contact_edit("agent").body
assert "health_alerts" in form and "checked" in form and "chat id" in form
assert "placeholder=" in web.contact_edit("").body

# ---- vessels ---------------------------------------------------------------------
def vessel(**kw):
    base = dict(original_key="", name="", mmsi="", imo="", port_captain=[], boarding_agent=[])
    base.update(kw)
    return web.vessel_save(**base)


assert "name or an MMSI" in err_of(vessel())
assert "9 digits" in err_of(vessel(name="X", mmsi="123"))
assert "7 digits" in err_of(vessel(name="X", mmsi="366000001", imo="12"))
assert vessel(name="=cmd|' /C calc'!A0", mmsi="366000001", port_captain=["capt"], boarding_agent=["agent"]).status_code == 303
v = store.load_vessels(V)[0]
assert not v["name"].startswith("="), v
assert "already on the list" in err_of(vessel(name="again", mmsi="366000001"))
page = web.vessels_page().body
assert "Capt Cruz" in page and "Agent Ames" in page
key = web.vkey(v)
assert vessel(original_key=key, name="Pacific Star", mmsi="366000001", port_captain=["capt"]).status_code == 303
v = store.load_vessels(V)[0]
assert v["name"] == "Pacific Star" and v["boarding_agents"] == [] and len(store.load_vessels(V)) == 1
assert "Pacific Star" in web.vessel_edit(key).body
assert web.vessels_csv().body.startswith("mmsi,imo,name")
# deleting a person unassigns them everywhere
web.contact_delete(id="capt")
v = store.load_vessels(V)[0]
assert v["port_captains"] == [] and "capt" not in {c["id"] for c in store.load_contacts(C)}
assert web.vessel_delete(key=key).status_code == 303 and store.load_vessels(V) == []

# ---- zones -----------------------------------------------------------------------
vals = web.zone_values(store.load_zones(Z))
assert "Pilot station" in web.zones_page().body
ok = web.zones_save(**vals)
assert ok.status_code == 303, ok.body
r = web.zones_save(**dict(vals, ps_lat="33 37.2 N", ps_lon="118 15.6 W", ps_radius="2"))
assert r.status_code == 303
z = store.load_zones(Z)["zones"]["pilot_station"]
assert z["center"] == [33.62, -118.26] and z["radius_nm"] == 2.0, z
cases = {
    "bad number": (dict(ps_lat="abc"), "read latitude"),
    "wrong hemisphere letter": (dict(ps_lon="118 15.6 N"), "wrong N/S/E/W"),
    "positive west longitude": (dict(ps_lon="118.26"), "outside the AIS area box"),
    "huge radius": (dict(ps_radius="50"), "between 0.1 and 20"),
    "half an inbound ref": (dict(ps_in_lat="", ps_in_lon="-118.2"), "both latitude and longitude"),
    "one polygon point": (dict(pl_points="33.7, -118.2\\n33.6, -118.1"), "at least 3"),
    "bad polygon line": (dict(pl_points="33.7 -118.2\\n1\\n2"), "latitude, longitude"),
    "flipped box": (dict(bb_s="33.9", bb_n="33.3"), "south must be below north"),
    "tolerance": (dict(ps_tol="5"), "between 10 and 180"),
}
for label, (change, expect) in cases.items():
    resp = web.zones_save(**dict(vals, **{k: v.replace("\\n", "\n") for k, v in change.items()}))
    assert resp.status_code == 400 and expect in resp.body, (label, resp.status_code, resp.body[-300:])
after = store.load_zones(Z)
assert after["zones"]["pilot_station"]["radius_nm"] == 2.0, "failed saves must not change the file"
# clearing the inbound reference removes the check
web.zones_save(**dict(vals, ps_in_lat="", ps_in_lon=""))
assert "inbound" not in store.load_zones(Z)["zones"]["pilot_station"]

# ---- status ----------------------------------------------------------------------
H = web.HEALTH
assert "isn't reporting" in web.status_page().body
import json  # noqa: E402
now = time.time()
good = {"updated_at": now, "started_at": now - 7200, "ok": True, "problems": [], "connected": True,
        "last_message_age_s": 3.0, "messages": 12345, "reconnects": 1, "vessels": 7, "alerts_sent": 2,
        "last_alert": {"vessel": "PACIFIC STAR", "zone": "LA/LB Pilot Station", "time": now - 600},
        "delivery_failures": {}}
json.dump(good, open(H, "w"))
b = web.status_page().body
assert "Everything looks fine" in b and "PACIFIC STAR" in b and "12,345" in b and "3s ago" in b, b
json.dump(dict(good, ok=False, problems=["AIS feed silent: no reports for 12 min"], connected=False), open(H, "w"))
b = web.status_page().body
assert "There is a problem" in b and "AIS feed silent" in b and "not connected" in b
json.dump(dict(good, updated_at=now - 300), open(H, "w"))
assert "isn't reporting" in web.status_page().body and "5 min ago" in web.status_page().body
print("WEB LOGIC OK")
