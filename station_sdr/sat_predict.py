"""Prévision des passages de satellites (SGP4) au-dessus de la station.

Éléments orbitaux (TLE) : CelesTrak, groupe « amateur » (+ ISS), mis en
cache sur le PC et rafraîchis toutes les 12 heures.
"""

import json
import math
import time
from pathlib import Path

import requests
from sgp4.api import Satrec, jday
from sgp4.propagation import gstime

TLE_URLS = [
    "https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=tle",
    "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=tle",  # ISS
]
RE_KM = 6378.137
F_WGS = 1 / 298.257223563
OMEGA_E = 7.2921150e-5  # rad/s

# Satellites radioamateurs suivis par défaut : (motif dans le nom CelesTrak,
# nom affiché, fréquences utiles).
FAVORITES = [
    ("ISS", "ISS", "APRS 145,825 · phonie 437,800 / 145,800 · SSTV 145,800"),
    ("SO-50", "SO-50", "FM : montée 145,850 (67 Hz) · descente 436,795"),
    ("AO-91", "AO-91", "FM : montée 435,250 (67 Hz) · descente 145,960"),
    ("PO-101", "PO-101 (Diwata-2)", "FM : montée 437,500 (141,3 Hz) · descente 145,900"),
    ("IO-117", "IO-117 (GreenCube)", "Digipeater 435,310"),
    ("RS-44", "RS-44", "Transpondeur SSB/CW : 145,935–145,995 ↑ / 435,610–435,670 ↓"),
    ("AO-7", "AO-7", "Transpondeur mode B : 432,125–432,175 ↑ / 145,975–145,925 ↓"),
    ("AO-123", "AO-123 (ASRTU-1)", "FM : montée 145,850 · descente 435,400"),
    ("SO-124", "SO-124 (HADES-R)", "FM : montée 145,875 · descente 436,666"),
    ("SO-125", "SO-125 (HADES-ICM)", "FM : montée 145,875 · descente 436,666"),
]


def _cache_file() -> Path:
    from .plat import cache_dir
    p = cache_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p / "tle_cache.json"


def load_tles(force=False):
    """[(nom, ligne1, ligne2)] ; cache local de 12 h, conservé hors ligne."""
    cache = _cache_file()
    data = None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if data and not force and time.time() - data.get("time", 0) < 12 * 3600:
        return data["tles"], data["time"]
    tles, seen = [], set()
    try:
        for url in TLE_URLS:
            txt = requests.get(url, timeout=20, headers={"User-Agent": "STATION-SDR (F4JCF)"}).text
            lines = [l.rstrip() for l in txt.splitlines() if l.strip()]
            for i in range(0, len(lines) - 2, 3):
                name, l1, l2 = lines[i].strip(), lines[i + 1], lines[i + 2]
                if l1.startswith("1 ") and l2.startswith("2 ") and l1[2:7] not in seen:
                    seen.add(l1[2:7])
                    tles.append((name, l1, l2))
        if tles:
            cache.write_text(json.dumps({"time": time.time(), "tles": tles}), encoding="utf-8")
            return tles, time.time()
    except Exception:
        pass
    if data:
        return data["tles"], data["time"]  # hors ligne : anciens éléments
    raise RuntimeError("éléments orbitaux indisponibles (pas d'accès à CelesTrak)")


def favorite_info(name):
    up = name.upper()
    for key, label, freqs in FAVORITES:
        if key == "ISS" and ("ISS (ZARYA)" in up or up.startswith("ISS")):
            return label, freqs
        if key != "ISS" and (f"({key})" in up or up.startswith(key)):
            return label, freqs
    return None, None


class Observer:
    def __init__(self, lat, lon, alt_m=100.0):
        self.lat, self.lon = math.radians(lat), math.radians(lon)
        e2 = F_WGS * (2 - F_WGS)
        n = RE_KM / math.sqrt(1 - e2 * math.sin(self.lat) ** 2)
        h = alt_m / 1000
        self.ecef = ((n + h) * math.cos(self.lat) * math.cos(self.lon),
                     (n + h) * math.cos(self.lat) * math.sin(self.lon),
                     (n * (1 - e2) + h) * math.sin(self.lat))


class Sat:
    def __init__(self, name, l1, l2):
        self.name = name
        self.rec = Satrec.twoline2rv(l1, l2)
        self.label, self.freqs = favorite_info(name)

    def ecef(self, t):
        """Position et vitesse ECEF (km, km/s) à l'instant t (epoch Unix)."""
        tm = time.gmtime(t)
        jd, fr = jday(tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec + (t % 1))
        err, r, v = self.rec.sgp4(jd, fr)
        if err:
            return None, None
        g = gstime(jd + fr)
        c, s = math.cos(g), math.sin(g)
        x, y, z = c * r[0] + s * r[1], -s * r[0] + c * r[1], r[2]
        vx, vy, vz = c * v[0] + s * v[1], -s * v[0] + c * v[1], v[2]
        vx, vy = vx + OMEGA_E * y, vy - OMEGA_E * x  # repère terrestre tournant
        return (x, y, z), (vx, vy, vz)

    def look(self, obs, t):
        """(azimut°, élévation°, distance km, vitesse radiale km/s)."""
        r, v = self.ecef(t)
        if r is None:
            return None
        dx, dy, dz = r[0] - obs.ecef[0], r[1] - obs.ecef[1], r[2] - obs.ecef[2]
        sl, cl, so, co = math.sin(obs.lat), math.cos(obs.lat), math.sin(obs.lon), math.cos(obs.lon)
        e = -so * dx + co * dy
        n = -sl * co * dx - sl * so * dy + cl * dz
        u = cl * co * dx + cl * so * dy + sl * dz
        rng = math.sqrt(dx * dx + dy * dy + dz * dz)
        az = (math.degrees(math.atan2(e, n)) + 360) % 360
        el = math.degrees(math.asin(u / rng))
        rr = (dx * v[0] + dy * v[1] + dz * v[2]) / rng
        return az, el, rng, rr

    def subpoint(self, t):
        r, _v = self.ecef(t)
        if r is None:
            return None
        x, y, z = r
        lon = math.degrees(math.atan2(y, x))
        p = math.hypot(x, y)
        e2 = F_WGS * (2 - F_WGS)
        lat = math.atan2(z, p * (1 - e2))
        for _ in range(5):
            n = RE_KM / math.sqrt(1 - e2 * math.sin(lat) ** 2)
            h = p / math.cos(lat) - n
            lat = math.atan2(z, p * (1 - e2 * n / (n + h)))
        n = RE_KM / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        return math.degrees(lat), lon, h

    def passes(self, obs, start, hours=24, min_el=10, step=30):
        """Liste de passages : AOS, LOS, élévation max et instant, azimuts."""
        out, t, end = [], start, start + hours * 3600
        prev = self.look(obs, t)
        if prev is None:
            return out
        cur = None
        while t < end:
            t2 = t + step
            lk = self.look(obs, t2)
            if lk is None:
                return out
            if prev[1] < 0 <= lk[1]:  # lever : affinage à 1 s
                a, b = t, t2
                while b - a > 1:
                    m = (a + b) / 2
                    if self.look(obs, m)[1] < 0:
                        a = m
                    else:
                        b = m
                cur = {"aos": b, "aos_az": self.look(obs, b)[0], "max_el": lk[1], "max_t": t2}
            if cur is not None and lk[1] > cur["max_el"]:
                cur["max_el"], cur["max_t"] = lk[1], t2
            if prev[1] >= 0 > lk[1] and cur is not None:
                a, b = t, t2
                while b - a > 1:
                    m = (a + b) / 2
                    if self.look(obs, m)[1] >= 0:
                        a = m
                    else:
                        b = m
                cur["los"], cur["los_az"] = a, self.look(obs, a)[0]
                # affinage de l'élévation maximale
                lo, hi = cur["max_t"] - step, cur["max_t"] + step
                for _ in range(20):
                    m1, m2 = lo + (hi - lo) / 3, hi - (hi - lo) / 3
                    if self.look(obs, m1)[1] < self.look(obs, m2)[1]:
                        lo = m1
                    else:
                        hi = m2
                cur["max_t"] = (lo + hi) / 2
                cur["max_el"] = self.look(obs, cur["max_t"])[1]
                cur["max_az"] = self.look(obs, cur["max_t"])[0]
                if cur["max_el"] >= min_el:
                    out.append(cur)
                cur = None
            prev, t = lk, t2
        return out


def footprint(sat, t, n=72):
    """Zone de visibilité (polygone lat/lon) autour du point sous le satellite."""
    sp = sat.subpoint(t)
    if sp is None:
        return []
    lat0, lon0, h = math.radians(sp[0]), math.radians(sp[1]), sp[2]
    ang = math.acos(RE_KM / (RE_KM + h))
    pts = []
    for i in range(n + 1):
        b = 2 * math.pi * i / n
        lat = math.asin(math.sin(lat0) * math.cos(ang) + math.cos(lat0) * math.sin(ang) * math.cos(b))
        lon = lon0 + math.atan2(math.sin(b) * math.sin(ang) * math.cos(lat0),
                                math.cos(ang) - math.sin(lat0) * math.sin(lat))
        pts.append((math.degrees(lat), (math.degrees(lon) + 540) % 360 - 180))
    return pts
