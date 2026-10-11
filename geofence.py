"""Small geofence helpers: circles and polygons, no external deps."""
import math

EARTH_NM = 3440.065  # earth radius in nautical miles


def distance_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_NM * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial bearing from point 1 to point 2, 0-360 true."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def angle_diff(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def point_in_polygon(lat, lon, poly):
    """Ray casting. poly is a list of (lat, lon)."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


class Zone:
    def __init__(self, name, cfg):
        self.name = name
        self.label = cfg.get("label", name)
        self.kind = cfg["type"]
        # Optional inbound-only filter: vessel must be heading toward `reference`
        inb = cfg.get("inbound")
        self.inbound_ref = tuple(inb["reference"]) if inb else None
        self.inbound_tol = float(inb.get("tolerance_deg", 60)) if inb else None
        if self.kind == "circle":
            self.lat, self.lon = cfg["center"]
            self.radius = float(cfg["radius_nm"])
            # exit margin so a ship drifting on the edge doesn't re-trigger
            self.exit_radius = self.radius * float(cfg.get("exit_factor", 1.5))
        elif self.kind == "polygon":
            self.poly = [tuple(p) for p in cfg["points"]]
        else:
            raise ValueError(f"unknown zone type {self.kind}")

    def contains(self, lat, lon):
        if self.kind == "circle":
            return distance_nm(lat, lon, self.lat, self.lon) <= self.radius
        return point_in_polygon(lat, lon, self.poly)

    def direction_ok(self, lat, lon, course):
        """True = inbound, False = outbound, None = can't tell yet.

        Inbound means the vessel's course points toward the reference point
        (the harbor entrance) within the tolerance. No filter configured -> True.
        """
        if self.inbound_ref is None:
            return True
        if course is None:
            return None
        want = bearing_deg(lat, lon, *self.inbound_ref)
        return angle_diff(course, want) <= self.inbound_tol

    def clearly_outside(self, lat, lon):
        """True once the vessel is far enough out to re-arm the alert."""
        if self.kind == "circle":
            return distance_nm(lat, lon, self.lat, self.lon) > self.exit_radius
        return not point_in_polygon(lat, lon, self.poly)
