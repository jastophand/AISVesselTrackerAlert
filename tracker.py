"""Core tracking engine: turns position reports into zone-entry events.

Kept separate from the websocket plumbing so it can be tested offline.
"""
import csv
import os
import sqlite3
import time

import yaml
from datetime import datetime, timezone

from geofence import Zone


def read_zones(path):
    """Return (zones_cfg, bounding_box) from the zones file."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    for k, v in data["zones"].items():
        Zone(k, v)  # validate before anyone uses it
    return data["zones"], data["bounding_box"]


def apply_zones(cfg):
    """If the config points at a zones file, load zones + AIS box from it."""
    if cfg.get("zones_file"):
        cfg["zones"], cfg["bounding_box"] = read_zones(cfg["zones_file"])


def norm_name(s):
    return " ".join((s or "").upper().split())


class Watchlist:
    def __init__(self, path):
        self.path = path
        self._load()

    def maybe_reload(self):
        """Re-read the CSV if it changed on disk. Returns True if it did."""
        if os.stat(self.path).st_mtime_ns == self._mtime:
            return False
        self._load()
        return True

    def _load(self):
        mtime = os.stat(self.path).st_mtime_ns
        by_mmsi, by_name = {}, {}
        with open(self.path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if not any(row.values()) or (row.get("mmsi") or "").startswith("#"):
                    continue
                v = {
                    "name": row.get("name", "").strip(),
                    "imo": row.get("imo", "").strip(),
                    "mmsi": row.get("mmsi", "").strip(),
                    "port_captains": _split(row.get("port_captain")),
                    "boarding_agents": _split(row.get("boarding_agent")),
                }
                if v["mmsi"]:
                    by_mmsi[v["mmsi"]] = v
                if v["name"]:
                    by_name[norm_name(v["name"])] = v
        # swap in the finished lists in one step so readers never see a half-loaded list
        self.by_mmsi, self.by_name, self._mtime = by_mmsi, by_name, mtime

    def lookup(self, mmsi, name):
        return self.by_mmsi.get(str(mmsi)) or self.by_name.get(norm_name(name))

    def mmsi_list(self):
        return list(self.by_mmsi)

    def all_have_mmsi(self):
        return all(v["mmsi"] for v in self.by_name.values())

    def count(self):
        """Number of distinct vessels on the list."""
        return len({id(v) for v in list(self.by_mmsi.values()) + list(self.by_name.values())})


def _split(s):
    return [p.strip() for p in (s or "").replace(",", ";").split(";") if p.strip()]


class Tracker:
    def __init__(self, cfg, watchlist, db_path="state.db"):
        self.zones = [Zone(k, v) for k, v in cfg["zones"].items()]
        self.watch = watchlist
        self.cooldown = float(cfg.get("cooldown_hours", 12)) * 3600
        self.alert_on_first_sight = cfg.get("alert_on_first_sight", False)
        # below this speed the course is ignored for the inbound check
        self.min_speed_kn = float(cfg.get("min_speed_knots", 2.0))
        self.db = sqlite3.connect(db_path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS zone_state ("
            "mmsi TEXT, zone TEXT, inside INTEGER, last_alert REAL, "
            "PRIMARY KEY (mmsi, zone))"
        )
        self.db.commit()

    def set_zones(self, zones_cfg):
        """Swap in new zone definitions (used when the zones file changes)."""
        self.zones = [Zone(k, v) for k, v in zones_cfg.items()]

    def _get(self, mmsi, zone):
        r = self.db.execute(
            "SELECT inside, last_alert FROM zone_state WHERE mmsi=? AND zone=?",
            (mmsi, zone),
        ).fetchone()
        return r  # None, or (inside, last_alert)

    def _set(self, mmsi, zone, inside, last_alert):
        self.db.execute(
            "INSERT OR REPLACE INTO zone_state VALUES (?,?,?,?)",
            (mmsi, zone, int(inside), last_alert),
        )
        self.db.commit()

    def _course(self, sog, cog, heading):
        """Best available direction of travel, or None if unreliable."""
        if sog is not None and sog < self.min_speed_kn:
            return None  # drifting / stopped, direction means nothing
        if cog is not None and cog < 360:
            return cog
        if heading is not None and heading < 360:
            return heading
        return None

    def process(self, mmsi, ship_name, lat, lon, sog=None, cog=None, heading=None, now=None):
        """Return a list of event dicts for newly entered zones (inbound only
        for zones that have an inbound filter)."""
        now = now or time.time()
        course = self._course(sog, cog, heading)
        mmsi = str(mmsi)
        vessel = self.watch.lookup(mmsi, ship_name)
        if not vessel:
            return []
        events = []
        for z in self.zones:
            prev = self._get(mmsi, z.name)
            inside_now = z.contains(lat, lon)
            if prev is None:
                # first sighting
                self._set(mmsi, z.name, inside_now, 0)
                if inside_now and self.alert_on_first_sight:
                    events.append(self._event(vessel, ship_name, mmsi, z, lat, lon, sog, now))
                    self._set(mmsi, z.name, True, now)
                continue
            was_inside, last_alert = prev
            if inside_now and not was_inside:
                if z.direction_ok(lat, lon, course) is not True:
                    # outbound or unknown: stay armed, re-check on the next report
                    continue
                if now - last_alert >= self.cooldown:
                    events.append(self._event(vessel, ship_name, mmsi, z, lat, lon, sog, now))
                    self._set(mmsi, z.name, True, now)
                else:
                    self._set(mmsi, z.name, True, last_alert)
            elif was_inside and z.clearly_outside(lat, lon):
                self._set(mmsi, z.name, False, last_alert)
        return events

    def _event(self, vessel, ship_name, mmsi, zone, lat, lon, sog, now):
        return {
            "vessel": vessel["name"] or ship_name or mmsi,
            "mmsi": mmsi,
            "imo": vessel["imo"],
            "zone": zone.name,
            "zone_label": zone.label,
            "lat": lat,
            "lon": lon,
            "sog": sog,
            "time_utc": datetime.fromtimestamp(now, timezone.utc),
            "port_captains": vessel["port_captains"],
            "boarding_agents": vessel["boarding_agents"],
        }
