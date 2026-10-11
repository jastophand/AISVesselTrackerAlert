"""Read/write vessels.csv and contacts.yaml safely.

Used by the web UI. The tracker only reads these files, and notices changes
by modification time, so everything here writes atomically under a lock.
"""
import csv
import fcntl
import io
import os
import re
import tempfile
from contextlib import contextmanager

import yaml

FIELDS = ["mmsi", "imo", "name", "port_captain", "boarding_agent"]
CONTACT_HEADER = (
    "# Who gets notified. Ids are referenced from vessels.csv.\n"
    "# role: port_captain, boarding_agent or other. channels: email, ntfy, telegram\n"
)


@contextmanager
def lock(path):
    with open(path + ".lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _atomic_write(path, text):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def _split(s):
    return [p.strip() for p in (s or "").replace(",", ";").split(";") if p.strip()]


def load_vessels(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not any((v or "").strip() for v in row.values()):
                continue
            if (row.get("mmsi") or "").startswith("#"):
                continue
            out.append({
                "mmsi": (row.get("mmsi") or "").strip(),
                "imo": (row.get("imo") or "").strip(),
                "name": (row.get("name") or "").strip(),
                "port_captains": _split(row.get("port_captain")),
                "boarding_agents": _split(row.get("boarding_agent")),
            })
    return out


def save_vessels(path, vessels):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(FIELDS)
    for v in vessels:
        w.writerow([
            v["mmsi"], v["imo"], v["name"],
            ";".join(v["port_captains"]), ";".join(v["boarding_agents"]),
        ])
    _atomic_write(path, buf.getvalue())


def load_contacts(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out = []
    for cid, c in data.items():
        c = dict(c or {})
        c["id"] = cid
        c.setdefault("name", cid)
        c.setdefault("role", "other")
        c.setdefault("channels", ["email"])
        out.append(c)
    return out


def save_contacts(path, contacts):
    data = {}
    for c in contacts:
        c = dict(c)
        cid = c.pop("id")
        data[cid] = {k: v for k, v in c.items() if v not in ("", None)}
    text = CONTACT_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    _atomic_write(path, text)


ZONES_HEADER = (
    "# Edited from the Zones page of the web UI (or by hand; the tracker reloads it on change).\n"
    "# Verify coordinates against the current NOAA chart / pilot notices.\n"
)


def load_zones(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_zones(path, data):
    _atomic_write(path, ZONES_HEADER + yaml.safe_dump(data, sort_keys=False))


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean_name(s, limit=50):
    """Tidy a vessel name: no control characters, no leading =+-@ (spreadsheet
    formulas), single spaces, bounded length."""
    s = _CTRL.sub(" ", s or "")
    s = " ".join(s.split()).lstrip("=+-@ ")
    return s[:limit].strip()
