"""Small admin UI for vessels and people.

    ADMIN_PASSWORD=... uvicorn web:app --host 0.0.0.0 --port 8080

Edits vessels.csv / contacts.yaml; the tracker picks changes up within seconds.
"""
import html
import os
import re
import secrets
from urllib.parse import quote, urlparse

import yaml
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import store
from settings import expand_env

CFG = expand_env(yaml.safe_load(open(os.environ.get("CONFIG", "config.yaml"), encoding="utf-8")))
VESSELS = CFG.get("watchlist_file", "data/vessels.csv")
CONTACTS = CFG.get("contacts_file", "data/contacts.yaml")
USER = os.environ.get("ADMIN_USER", "admin")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if not PASSWORD:
    raise SystemExit("Set ADMIN_PASSWORD before starting the web UI.")

security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)):
    ok_u = secrets.compare_digest(creds.username.encode(), USER.encode())
    ok_p = secrets.compare_digest(creds.password.encode(), PASSWORD.encode())
    if not (ok_u and ok_p):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


def same_origin(request: Request):
    """Basic auth is sent automatically by browsers, so block cross-site form posts."""
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "cross-site request blocked")


app = FastAPI(dependencies=[Depends(auth)], docs_url=None, redoc_url=None, openapi_url=None)
esc = html.escape
POST = [Depends(same_origin)]

CSS = """
:root{--bg:#fff;--fg:#1c2530;--mut:#6b7684;--line:#dde2e8;--acc:#0b5cab;--ok:#1a7f45;--err:#b3261e;--card:#f5f7fa}
@media(prefers-color-scheme:dark){:root{--bg:#12161b;--fg:#e6eaee;--mut:#97a1ad;--line:#2a323b;--acc:#6aa9ee;--ok:#5cc58a;--err:#f2867f;--card:#1a2027}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
nav{display:flex;gap:16px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line)}
nav a{color:var(--acc);text-decoration:none}main{max-width:960px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:8px 0 12px}table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:13px}.muted{color:var(--mut)}
.ok{color:var(--ok)}.err{color:var(--err)}a{color:var(--acc)}
.btn,button{background:var(--acc);color:var(--bg);border:0;border-radius:6px;padding:7px 12px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}
button.link{background:none;color:var(--err);padding:0}
form.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;max-width:560px}
label{display:block;margin:10px 0 3px;font-weight:600}input[type=text]{width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
select{padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
.chk{display:inline-block;font-weight:400;margin:2px 14px 2px 0}.row{margin-top:16px}
@media(max-width:640px){th:nth-child(3),td:nth-child(3){display:none}}
"""


def page(title, body, msg="", err="", status=200):
    flash = (f'<p class="ok">{esc(msg)}</p>' if msg else "") + (f'<p class="err">{esc(err)}</p>' if err else "")
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
<style>{CSS}</style></head><body>
<nav><b>Vessel alerts</b><a href="/">Vessels</a><a href="/contacts">People</a><a href="/zones">Zones</a><a href="/status">Status</a></nav>
<main>{flash}{body}</main></body></html>""",
        status_code=status,
    )


def norm(s):
    return " ".join((s or "").upper().split())


def vkey(v):
    return v["mmsi"] or "name:" + norm(v["name"])


def checks(name, options, selected):
    out = []
    for val, label in options:
        c = " checked" if val in selected else ""
        out.append(f'<label class="chk"><input type="checkbox" name="{name}" value="{esc(val)}"{c}> {esc(label)}</label>')
    return "".join(out) or '<span class="muted">No one yet. Add people first.</span>'


# ---------------------------------------------------------------- vessels

@app.get("/", response_class=HTMLResponse)
def vessels_page(msg: str = "", err: str = ""):
    vessels = store.load_vessels(VESSELS)
    contacts = {c["id"]: c for c in store.load_contacts(CONTACTS)}

    def names(ids):
        parts = [esc(contacts[i]["name"]) if i in contacts else f'<span class="err">{esc(i)}?</span>' for i in ids]
        return ", ".join(parts) or '<span class="muted">none</span>'

    rows = ""
    for v in sorted(vessels, key=lambda v: norm(v["name"]) or v["mmsi"]):
        k = quote(vkey(v), safe="")
        rows += (
            f"<tr><td>{esc(v['name'] or '(no name)')}</td><td>{esc(v['mmsi'])}</td><td>{esc(v['imo'])}</td>"
            f"<td>{names(v['port_captains'])}</td><td>{names(v['boarding_agents'])}</td>"
            f'<td><a href="/vessels/edit?key={k}">Edit</a> '
            f'<form method="post" action="/vessels/delete" style="display:inline" '
            f"onsubmit=\"return confirm('Remove this vessel?')\">"
            f'<input type="hidden" name="key" value="{esc(vkey(v))}"><button class="link">Delete</button></form></td></tr>'
        )
    if not rows:
        rows = '<tr><td colspan="6" class="muted">No vessels yet.</td></tr>'
    body = (
        f"<h1>Vessels ({len(vessels)})</h1>"
        '<p><a class="btn" href="/vessels/edit">Add vessel</a> &nbsp; <a href="/vessels.csv">Download CSV</a></p>'
        "<table><tr><th>Name</th><th>MMSI</th><th>IMO</th><th>Port captain</th><th>Boarding agent</th><th></th></tr>"
        f"{rows}</table>"
    )
    return page("Vessels", body, msg, err)


def vessel_form(v, original_key="", err="", status=200):
    contacts = store.load_contacts(CONTACTS)
    caps = [(c["id"], c["name"]) for c in contacts if c["role"] != "boarding_agent"]
    agents = [(c["id"], c["name"]) for c in contacts if c["role"] != "port_captain"]
    body = f"""<h1>{'Edit' if original_key else 'Add'} vessel</h1>
<form class="card" method="post" action="/vessels/save">
<input type="hidden" name="original_key" value="{esc(original_key)}">
<label>Vessel name</label><input type="text" name="name" value="{esc(v['name'])}">
<label>MMSI (9 digits, best for matching)</label><input type="text" name="mmsi" value="{esc(v['mmsi'])}" inputmode="numeric">
<label>IMO (optional)</label><input type="text" name="imo" value="{esc(v['imo'])}" inputmode="numeric">
<label>Port captain(s)</label>{checks('port_captain', caps, v['port_captains'])}
<label>Boarding agent(s)</label>{checks('boarding_agent', agents, v['boarding_agents'])}
<div class="row"><button>Save</button> &nbsp; <a href="/">Cancel</a></div></form>"""
    return page("Vessel", body, err=err, status=status)


@app.get("/vessels/edit", response_class=HTMLResponse)
def vessel_edit(key: str = ""):
    if not key:
        return vessel_form({"name": "", "mmsi": "", "imo": "", "port_captains": [], "boarding_agents": []})
    for v in store.load_vessels(VESSELS):
        if vkey(v) == key:
            return vessel_form(v, key)
    raise HTTPException(404, "vessel not found")


@app.post("/vessels/save", dependencies=POST)
def vessel_save(
    original_key: str = Form(""),
    name: str = Form(""),
    mmsi: str = Form(""),
    imo: str = Form(""),
    port_captain: list[str] = Form(default=[]),
    boarding_agent: list[str] = Form(default=[]),
):
    v = {
        "name": store.clean_name(name),
        "mmsi": mmsi.strip(),
        "imo": imo.strip(),
        "port_captains": port_captain,
        "boarding_agents": boarding_agent,
    }
    err = None
    if not v["name"] and not v["mmsi"]:
        err = "Give the vessel a name or an MMSI."
    elif v["mmsi"] and not re.fullmatch(r"\d{9}", v["mmsi"]):
        err = "MMSI must be exactly 9 digits."
    elif v["imo"] and not re.fullmatch(r"\d{7}", v["imo"]):
        err = "IMO must be 7 digits."
    if err:
        return vessel_form(v, original_key, err, 400)
    with store.lock(VESSELS):
        vessels = store.load_vessels(VESSELS)
        others = [x for x in vessels if vkey(x) != original_key]
        if any(vkey(x) == vkey(v) for x in others):
            return vessel_form(v, original_key, "That vessel is already on the list.", 400)
        if original_key and len(others) < len(vessels):
            vessels = [v if vkey(x) == original_key else x for x in vessels]
        else:
            vessels.append(v)
        store.save_vessels(VESSELS, vessels)
    return RedirectResponse("/?msg=" + quote("Saved. The tracker picks it up in a few seconds."), 303)


@app.post("/vessels/delete", dependencies=POST)
def vessel_delete(key: str = Form(...)):
    with store.lock(VESSELS):
        vessels = [v for v in store.load_vessels(VESSELS) if vkey(v) != key]
        store.save_vessels(VESSELS, vessels)
    return RedirectResponse("/?msg=" + quote("Vessel removed."), 303)


@app.get("/vessels.csv")
def vessels_csv():
    with open(VESSELS, encoding="utf-8") as f:
        return Response(f.read(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="vessels.csv"'})


# ----------------------------------------------------------------- people

ROLES = [("port_captain", "Port captain"), ("boarding_agent", "Boarding agent"), ("other", "Other")]
CHANNELS = [("email", "Email"), ("ntfy", "ntfy push"), ("telegram", "Telegram")]


@app.get("/contacts", response_class=HTMLResponse)
def contacts_page(msg: str = "", err: str = ""):
    contacts = store.load_contacts(CONTACTS)
    rows = ""
    for c in contacts:
        bits = [t for k, t in (("always_notify", "every alert"), ("health_alerts", "health alerts"),
                               ("can_manage_vessels", "manages vessels")) if c.get(k)]
        tag = f" <span class='muted'>({esc(', '.join(bits))})</span>" if bits else ""
        rows += (
            f"<tr><td>{esc(c['name'])}<br><span class='muted'>{esc(c['id'])}</span></td>"
            f"<td>{esc(dict(ROLES).get(c['role'], c['role']))}</td>"
            f"<td>{esc(c.get('email', ''))}</td><td>{esc(', '.join(c['channels']))}{tag}</td>"
            f'<td><a href="/contacts/edit?id={quote(c["id"], safe="")}">Edit</a> '
            f'<form method="post" action="/contacts/delete" style="display:inline" '
            f"onsubmit=\"return confirm('Remove this person and unassign them from all vessels?')\">"
            f'<input type="hidden" name="id" value="{esc(c["id"])}"><button class="link">Delete</button></form></td></tr>'
        )
    if not rows:
        rows = '<tr><td colspan="5" class="muted">No one yet.</td></tr>'
    body = (
        "<h1>People</h1><p><a class='btn' href='/contacts/edit'>Add person</a></p>"
        "<table><tr><th>Name</th><th>Role</th><th>Email</th><th>Channels</th><th></th></tr>"
        f"{rows}</table>"
    )
    return page("People", body, msg, err)


def contact_form(c, is_edit, err="", status=200):
    roles = "".join(
        f'<option value="{r}"{" selected" if c["role"] == r else ""}>{esc(l)}</option>' for r, l in ROLES
    )
    id_field = (
        f'<input type="hidden" name="id" value="{esc(c["id"])}"><p class="muted">Id: {esc(c["id"])}</p>'
        if is_edit
        else f'<label>Short id (letters, numbers, - or _)</label><input type="text" name="id" value="{esc(c["id"])}">'
    )
    body = f"""<h1>{'Edit' if is_edit else 'Add'} person</h1>
<form class="card" method="post" action="/contacts/save">
<input type="hidden" name="is_edit" value="{'1' if is_edit else ''}">{id_field}
<label>Name</label><input type="text" name="name" value="{esc(c['name'])}">
<label>Role</label><select name="role">{roles}</select>
<label>Email</label><input type="text" name="email" value="{esc(c.get('email', ''))}">
<label>ntfy topic (for push alerts) <span class="muted">use a long random name, anyone who knows it can read it</span></label><input type="text" name="ntfy_topic" value="{esc(c.get('ntfy_topic', ''))}" placeholder="e.g. {secrets.token_urlsafe(12)}">
<label>Telegram chat id <span class="muted">they message the bot and send /start to see theirs</span></label><input type="text" name="telegram_chat_id" value="{esc(str(c.get('telegram_chat_id', '')))}">
<label>Send alerts by</label>{checks('channels', CHANNELS, c['channels'])}
<label class="chk" style="margin-top:12px"><input type="checkbox" name="always_notify" value="1"{' checked' if c.get('always_notify') else ''}> Send this person every alert, for every vessel</label>
<label class="chk"><input type="checkbox" name="health_alerts" value="1"{' checked' if c.get('health_alerts') else ''}> Send this person system health alerts (feed down, delivery failing)</label>
<label class="chk"><input type="checkbox" name="can_manage_vessels" value="1"{' checked' if c.get('can_manage_vessels') else ''}> Let this person add and remove vessels from Telegram</label>
<div class="row"><button>Save</button> &nbsp; <a href="/contacts">Cancel</a></div></form>"""
    return page("Person", body, err=err, status=status)


@app.get("/contacts/edit", response_class=HTMLResponse)
def contact_edit(id: str = ""):
    if not id:
        return contact_form({"id": "", "name": "", "role": "port_captain", "channels": ["email"]}, False)
    for c in store.load_contacts(CONTACTS):
        if c["id"] == id:
            return contact_form(c, True)
    raise HTTPException(404, "person not found")


@app.post("/contacts/save", dependencies=POST)
def contact_save(
    is_edit: str = Form(""),
    id: str = Form(""),
    name: str = Form(""),
    role: str = Form("other"),
    email: str = Form(""),
    ntfy_topic: str = Form(""),
    telegram_chat_id: str = Form(""),
    channels: list[str] = Form(default=[]),
    always_notify: str = Form(""),
    health_alerts: str = Form(""),
    can_manage_vessels: str = Form(""),
):
    c = {
        "id": id.strip().lower(), "name": " ".join(name.split()), "role": role,
        "email": email.strip(), "ntfy_topic": ntfy_topic.strip(),
        "telegram_chat_id": telegram_chat_id.strip(), "channels": channels,
        "always_notify": True if always_notify else "",
        "health_alerts": True if health_alerts else "",
        "can_manage_vessels": True if can_manage_vessels else "",
    }
    editing = bool(is_edit)
    err = None
    if not re.fullmatch(r"[a-z0-9_-]{2,40}", c["id"]):
        err = "Id must be 2-40 characters: letters, numbers, - or _."
    elif not c["name"]:
        err = "Name is required."
    elif role not in dict(ROLES):
        err = "Pick a role."
    elif not channels:
        err = "Pick at least one way to reach them."
    elif "email" in channels and "@" not in c["email"]:
        err = "Email alerts need an email address."
    elif "ntfy" in channels and not c["ntfy_topic"]:
        err = "ntfy alerts need a topic."
    elif "telegram" in channels and not c["telegram_chat_id"]:
        err = "Telegram alerts need a chat id."
    elif c["telegram_chat_id"] and not re.fullmatch(r"-?\d{5,15}", c["telegram_chat_id"]):
        err = "A Telegram chat id is just digits. Message the bot and send /start to see yours."
    elif c["can_manage_vessels"] and not c["telegram_chat_id"]:
        err = "Managing vessels from Telegram needs their Telegram chat id."
    if err:
        return contact_form(c, editing, err, 400)
    with store.lock(VESSELS):
        contacts = store.load_contacts(CONTACTS)
        exists = any(x["id"] == c["id"] for x in contacts)
        if exists and not editing:
            return contact_form(c, False, "That id is already taken.", 400)
        if editing and not exists:
            raise HTTPException(404, "person not found")
        if editing:
            contacts = [c if x["id"] == c["id"] else x for x in contacts]
        else:
            contacts.append(c)
        store.save_contacts(CONTACTS, contacts)
    return RedirectResponse("/contacts?msg=" + quote("Saved."), 303)


@app.post("/contacts/delete", dependencies=POST)
def contact_delete(id: str = Form(...)):
    with store.lock(VESSELS):
        store.save_contacts(CONTACTS, [c for c in store.load_contacts(CONTACTS) if c["id"] != id])
        vessels = store.load_vessels(VESSELS)
        for v in vessels:
            v["port_captains"] = [x for x in v["port_captains"] if x != id]
            v["boarding_agents"] = [x for x in v["boarding_agents"] if x != id]
        store.save_vessels(VESSELS, vessels)
    return RedirectResponse("/contacts?msg=" + quote("Person removed and unassigned from vessels."), 303)


# ------------------------------------------------------------------ zones

ZONES = CFG.get("zones_file", "data/zones.yaml")


def parse_coord(text, kind):
    """Accept 33.6417, -118.26, 33 38.5 N, 33°38'30"N, 118-15-36W and similar."""
    s = (text or "").strip().upper()
    if not s:
        raise ValueError(f"{kind} is empty")
    hemi = re.findall(r"[NSEW]", s)
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if not nums or len(nums) > 3 or len(hemi) > 1:
        raise ValueError(f"can't read {kind} '{text}'")
    if hemi and hemi[0] not in ("NS" if kind == "latitude" else "EW"):
        raise ValueError(f"{kind} '{text}' has the wrong N/S/E/W letter")
    if any(float(n) >= 60 for n in nums[1:]):
        raise ValueError(f"minutes/seconds in '{text}' must be under 60")
    val = sum(float(n) / 60 ** i for i, n in enumerate(nums))
    if s.startswith("-") or (hemi and hemi[0] in "SW"):
        val = -val
    if abs(val) > (90 if kind == "latitude" else 180):
        raise ValueError(f"{kind} '{text}' is out of range")
    return round(val, 6)


def parse_pair(lat, lon):
    return parse_coord(lat, "latitude"), parse_coord(lon, "longitude")


def _f(v):
    return f"{v:.5f}"


def zone_values(data):
    z, bb = data["zones"], data["bounding_box"]
    ps, pl = z["pilot_station"], z["port_limits"]
    psi, pli = ps.get("inbound"), pl.get("inbound")
    return {
        "ps_lat": _f(ps["center"][0]), "ps_lon": _f(ps["center"][1]),
        "ps_radius": str(ps["radius_nm"]),
        "ps_in_lat": _f(psi["reference"][0]) if psi else "", "ps_in_lon": _f(psi["reference"][1]) if psi else "",
        "ps_tol": str(psi["tolerance_deg"]) if psi else "60",
        "pl_points": "\n".join(f"{_f(p[0])}, {_f(p[1])}" for p in pl["points"]),
        "pl_in_lat": _f(pli["reference"][0]) if pli else "", "pl_in_lon": _f(pli["reference"][1]) if pli else "",
        "pl_tol": str(pli["tolerance_deg"]) if pli else "75",
        "bb_s": _f(bb[0][0]), "bb_w": _f(bb[0][1]), "bb_n": _f(bb[1][0]), "bb_e": _f(bb[1][1]),
    }


def zones_form(vals, saved, msg="", err="", status=200):
    def inp(name, label, hint=""):
        h = f' <span class="muted">{esc(hint)}</span>' if hint else ""
        return f'<label>{esc(label)}{h}</label><input type="text" name="{name}" value="{esc(vals[name])}">'

    ps_lat, ps_lon = saved["zones"]["pilot_station"]["center"]
    body = f"""<h1>Zones</h1>
<p class="muted">Enter coordinates as decimal degrees (33.62, -118.26) or degrees and minutes (33 37.2 N, 118 15.6 W).
The tracker applies changes within a few seconds. Check them against the current NOAA chart or pilot notices.</p>
<form class="card" method="post" action="/zones/save">
<h2>Pilot station</h2>
{inp('ps_lat', 'Latitude')}{inp('ps_lon', 'Longitude')}
<p><a target="_blank" rel="noopener" href="https://www.google.com/maps?q={ps_lat},{ps_lon}">Saved position on a map</a></p>
{inp('ps_radius', 'Alert radius (nautical miles)')}
<label>Inbound check: harbor entrance the ship should be heading toward</label>
<p class="muted">Leave both blank to alert on any direction.</p>
{inp('ps_in_lat', 'Entrance latitude')}{inp('ps_in_lon', 'Entrance longitude')}
{inp('ps_tol', 'Tolerance (degrees off a straight line to the entrance)')}
<h2>Port limits</h2>
<label>Corner points, one per line as "latitude, longitude" <span class="muted">(at least 3, in order around the boundary)</span></label>
<textarea name="pl_points" rows="6" style="width:100%;font:inherit;padding:8px">{esc(vals['pl_points'])}</textarea>
{inp('pl_in_lat', 'Inbound reference latitude')}{inp('pl_in_lon', 'Inbound reference longitude')}
{inp('pl_tol', 'Tolerance (degrees)')}
<h2>AIS area box</h2>
<p class="muted">The box AISStream sends us. It must be big enough to include the pilot station and the approach to it.</p>
{inp('bb_s', 'South edge (latitude)')}{inp('bb_n', 'North edge (latitude)')}
{inp('bb_w', 'West edge (longitude)')}{inp('bb_e', 'East edge (longitude)')}
<div class="row"><button>Save zones</button></div></form>"""
    return page("Zones", body, msg, err, status)


@app.get("/zones", response_class=HTMLResponse)
def zones_page(msg: str = "", err: str = ""):
    data = store.load_zones(ZONES)
    return zones_form(zone_values(data), data, msg, err)


@app.post("/zones/save", dependencies=POST)
def zones_save(
    ps_lat: str = Form(""), ps_lon: str = Form(""), ps_radius: str = Form(""),
    ps_in_lat: str = Form(""), ps_in_lon: str = Form(""), ps_tol: str = Form(""),
    pl_points: str = Form(""),
    pl_in_lat: str = Form(""), pl_in_lon: str = Form(""), pl_tol: str = Form(""),
    bb_s: str = Form(""), bb_w: str = Form(""), bb_n: str = Form(""), bb_e: str = Form(""),
):
    vals = {k: v for k, v in locals().items() if k in zone_values_keys}
    try:
        south, west = parse_pair(bb_s, bb_w)
        north, east = parse_pair(bb_n, bb_e)
        if not (south < north and west < east):
            raise ValueError("Area box: south must be below north and west must be left of east.")
        center = parse_pair(ps_lat, ps_lon)
        if not (south <= center[0] <= north and west <= center[1] <= east):
            raise ValueError("The pilot station is outside the AIS area box. Check the signs "
                             "(West longitudes are negative) or widen the box.")
        try:
            radius = float(ps_radius)
        except ValueError:
            raise ValueError("Alert radius must be a number.")
        if not 0.1 <= radius <= 20:
            raise ValueError("Alert radius should be between 0.1 and 20 nautical miles.")

        def inbound(lat, lon, tol, default):
            if not lat.strip() and not lon.strip():
                return None
            if not lat.strip() or not lon.strip():
                raise ValueError("Fill in both latitude and longitude for the inbound reference, or leave both blank.")
            ref = parse_pair(lat, lon)
            try:
                t = float(tol or default)
            except ValueError:
                raise ValueError("Tolerance must be a number of degrees.")
            if not 10 <= t <= 180:
                raise ValueError("Tolerance should be between 10 and 180 degrees.")
            return {"reference": list(ref), "tolerance_deg": t}

        ps_in = inbound(ps_in_lat, ps_in_lon, ps_tol, 60)
        pl_in = inbound(pl_in_lat, pl_in_lon, pl_tol, 75)
        points = []
        for line in pl_points.splitlines():
            if not line.strip():
                continue
            if line.count(",") != 1:
                raise ValueError(f"Port limit point '{line.strip()}' should look like: latitude, longitude")
            a, b = line.split(",")
            points.append(list(parse_pair(a, b)))
        if len(points) < 3:
            raise ValueError("Port limits need at least 3 corner points.")
    except ValueError as e:
        data = store.load_zones(ZONES)
        return zones_form(vals, data, err=str(e), status=400)

    with store.lock(VESSELS):
        data = store.load_zones(ZONES)
        z = data["zones"]
        z["pilot_station"]["center"] = list(center)
        z["pilot_station"]["radius_nm"] = radius
        z["port_limits"]["points"] = points
        for zone, ib in ((z["pilot_station"], ps_in), (z["port_limits"], pl_in)):
            if ib is None:
                zone.pop("inbound", None)
            else:
                zone["inbound"] = ib
        data["bounding_box"] = [[south, west], [north, east]]
        store.save_zones(ZONES, data)
    return RedirectResponse("/zones?msg=" + quote("Saved. The tracker applies it within a few seconds."), 303)


zone_values_keys = {
    "ps_lat", "ps_lon", "ps_radius", "ps_in_lat", "ps_in_lon", "ps_tol", "pl_points",
    "pl_in_lat", "pl_in_lon", "pl_tol", "bb_s", "bb_w", "bb_n", "bb_e",
}


# ----------------------------------------------------------------- status

import json as _json
import time as _time
from datetime import datetime as _dt
from zoneinfo import ZoneInfo as _Zi

HEALTH = CFG.get("health_file", "data/health.json")


def _ago(sec):
    sec = int(sec)
    if sec < 90:
        return f"{sec}s ago"
    if sec < 5400:
        return f"{sec // 60} min ago"
    if sec < 172800:
        return f"{sec // 3600} h ago"
    return f"{sec // 86400} days ago"


def _local(ts):
    try:
        tz = _Zi(CFG.get("timezone") or "America/Los_Angeles")
    except Exception:
        tz = None
    return _dt.fromtimestamp(ts, tz).strftime("%a %b %d %H:%M")


@app.get("/status", response_class=HTMLResponse)
def status_page():
    try:
        with open(HEALTH, encoding="utf-8") as f:
            d = _json.load(f)
    except (OSError, ValueError):
        d = None
    refresh = "<script>setTimeout(function(){location.reload()},30000)</script>"
    if d is None or _time.time() - d.get("updated_at", 0) > 90:
        since = f" Last report {_ago(_time.time() - d['updated_at'])}." if d else ""
        body = ("<h1>Status</h1><p class='err'><b>The tracker isn't reporting.</b>" + esc(since) +
                " It may be stopped, restarting, or not deployed. Check the tracker container's logs.</p>" + refresh)
        return page("Status", body)
    head = ("<p class='ok'><b>Everything looks fine.</b></p>" if d.get("ok") else
            "<p class='err'><b>There is a problem.</b></p><ul>" +
            "".join(f"<li>{esc(p)}</li>" for p in d.get("problems", [])) + "</ul>")
    age = d.get("last_message_age_s")
    if d.get("connected"):
        feed = "connected" + (f", last position report {_ago(age)}" if age is not None else ", waiting for the first report")
    else:
        feed = "not connected"
    la = d.get("last_alert")
    last = f"{la['vessel']} at {la['zone']}, {_local(la['time'])}" if la else "none yet"
    fails = d.get("delivery_failures") or {}
    rows = [
        ("AIS feed", feed),
        ("Position reports since start", f"{d.get('messages', 0):,}"),
        ("Vessels watched", str(d.get("vessels", 0))),
        ("Alerts sent since start", str(d.get("alerts_sent", 0))),
        ("Last alert", last),
        ("Delivery trouble", ", ".join(f"{k}: {v} in a row" for k, v in fails.items()) or "none"),
        ("Tracker running since", _local(d.get("started_at", 0))),
        ("Reconnects", str(max(d.get("reconnects", 1) - 1, 0))),
    ]
    table = "<table>" + "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in rows) + "</table>"
    return page("Status", "<h1>Status</h1>" + head + table + refresh)
