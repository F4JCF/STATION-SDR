"""Goniométrie (recherche de balise type SATER) : lecture de positions,
tracé des relèvements et estimation de la zone d'émission.

Calculs dans un plan local (projection équirectangulaire autour des
observateurs) : largement suffisant sur quelques dizaines de kilomètres.
"""

import math
import re

R_EARTH = 6371.0


def parse_position(text: str):
    """(lat, lon) depuis « 43.5905, 7.0021 », « 43,5905 7,0021 »,
    « 43°35'26"N 7°00'07"E », « N 43 35.43 E 7 0.12 » ou un locator
    (JN33LP, JN33LP12). Lève ValueError sinon."""
    t = (text or "").strip().upper()
    loc = re.fullmatch(r"([A-R]{2})(\d{2})([A-X]{2})?(\d{2})?", t.replace(" ", ""))
    if loc:
        return _locator(loc)
    if re.search(r"[NSEWO]", t):
        t = t.replace("O", "W")
        toks = re.findall(r"[NSEW]|\d+(?:[.,]\d+)?", t)
        groups, cur = [], []
        prefix = toks[0] in "NSEW" if toks else False
        for tok in toks:
            if tok in ("N", "S", "E", "W"):
                if prefix:
                    cur = [tok]
                    groups.append(cur)
                else:
                    groups.append([tok] + cur)
                    cur = []
            else:
                cur.append(tok)
        lat = lon = None
        for g in groups:
            hemi, nums = g[0], [float(x.replace(",", ".")) for x in g[1:4]]
            if not nums:
                continue
            v = nums[0] + (nums[1] / 60 if len(nums) > 1 else 0) + (nums[2] / 3600 if len(nums) > 2 else 0)
            if hemi in "NS" and lat is None:
                lat = v if hemi == "N" else -v
            elif hemi in "EW" and lon is None:
                lon = v if hemi == "E" else -v
        if lat is None or lon is None:
            raise ValueError("position incomplète")
        return _check(lat, lon)
    nums = re.findall(r"-?\d+(?:[.,]\d+)?", t)
    if len(nums) != 2:
        raise ValueError("position non reconnue")
    return _check(float(nums[0].replace(",", ".")), float(nums[1].replace(",", ".")))


def _locator(m):
    a, d, s, e = m.group(1), m.group(2), m.group(3), m.group(4)
    lon = (ord(a[0]) - 65) * 20 - 180 + int(d[0]) * 2
    lat = (ord(a[1]) - 65) * 10 - 90 + int(d[1])
    w, h = 2.0, 1.0
    if s:
        lon += (ord(s[0]) - 65) * 5 / 60
        lat += (ord(s[1]) - 65) * 2.5 / 60
        w, h = 5 / 60, 2.5 / 60
        if e:
            lon += int(e[0]) * 0.5 / 60
            lat += int(e[1]) * 0.25 / 60
            w, h = 0.5 / 60, 0.25 / 60
    return _check(lat + h / 2, lon + w / 2)


def _check(lat, lon):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("coordonnées hors limites")
    return lat, lon


def destination(lat, lon, az_deg, dist_km):
    p1, l1, b, d = math.radians(lat), math.radians(lon), math.radians(az_deg), dist_km / R_EARTH
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def dist_bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * R_EARTH * math.asin(math.sqrt(min(1.0, a)))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return d, (math.degrees(math.atan2(y, x)) + 360) % 360


def circle(lat, lon, r_km, n=48):
    return [destination(lat, lon, 360 * i / n, r_km) for i in range(n + 1)]


def estimate(bearings):
    """bearings : [(lat, lon, azimut vrai °)]. Renvoie None (moins de deux
    relèvements exploitables) ou un dict : lat, lon, radius_km (zone
    probable), min_angle (angle d'intersection le plus faible, °),
    crossings [(lat, lon)], behind (relèvements qui « tournent le dos » au
    point estimé), quality ("bonne", "moyenne", "faible")."""
    if len(bearings) < 2:
        return None
    lat0 = sum(b[0] for b in bearings) / len(bearings)
    lon0 = sum(b[1] for b in bearings) / len(bearings)
    kx = math.cos(math.radians(lat0)) * math.pi * R_EARTH / 180
    ky = math.pi * R_EARTH / 180

    def to_xy(lat, lon):
        return (lon - lon0) * kx, (lat - lat0) * ky

    def to_ll(x, y):
        return lat0 + y / ky, lon0 + x / kx

    lines = []
    for lat, lon, az in bearings:
        px, py = to_xy(lat, lon)
        a = math.radians(az)
        lines.append((px, py, math.sin(a), math.cos(a)))

    # Croisements deux à deux (devant les deux observateurs)
    crossings, angles = [], []
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            x1, y1, dx1, dy1 = lines[i]
            x2, y2, dx2, dy2 = lines[j]
            den = dx1 * dy2 - dy1 * dx2
            ang = math.degrees(math.asin(min(1.0, abs(den))))
            if abs(den) < 1e-6:
                continue
            t1 = ((x2 - x1) * dy2 - (y2 - y1) * dx2) / den
            t2 = ((x2 - x1) * dy1 - (y2 - y1) * dx1) / den
            if t1 > 0 and t2 > 0:
                crossings.append(((x1 + t1 * dx1, y1 + t1 * dy1), ang))
                angles.append(ang)
    if not crossings:
        return {"lat": None, "lon": None, "radius_km": None, "min_angle": None, "crossings": [], "behind": [],
                "quality": "aucun croisement devant les équipes"}

    # Point le plus proche de toutes les droites (moindres carrés), pondéré
    # par l'éloignement : un relèvement lointain compte autant qu'un proche.
    a11 = a12 = a22 = b1 = b2 = 0.0
    for px, py, dx, dy in lines:
        m11, m12, m22 = 1 - dx * dx, -dx * dy, 1 - dy * dy
        a11 += m11
        a12 += m12
        a22 += m22
        b1 += m11 * px + m12 * py
        b2 += m12 * px + m22 * py
    det = a11 * a22 - a12 * a12
    if abs(det) < 1e-9:
        (ex, ey), _ = crossings[0]
    else:
        ex, ey = (a22 * b1 - a12 * b2) / det, (a11 * b2 - a12 * b1) / det
    behind, perp, dists = [], [], []
    for k, (px, py, dx, dy) in enumerate(lines):
        vx, vy = ex - px, ey - py
        along = vx * dx + vy * dy
        if along < 0:
            behind.append(k)
        perp.append(abs(vx * dy - vy * dx))
        dists.append(math.hypot(vx, vy))
    min_angle = min(angles) if angles else None
    if len(lines) >= 3:
        rms = math.sqrt(sum(p * p for p in perp) / len(perp))
        radius = max(0.3, 2 * rms, max(dists) * math.tan(math.radians(3)))
    else:
        # deux relèvements : erreur supposée ±5° sur chacun
        s = math.sin(math.radians(max(min_angle or 1, 1)))
        radius = max(0.3, (sum(dists) / len(dists)) * math.tan(math.radians(5)) / s)
    quality = ("bonne" if len(lines) >= 3 and (min_angle or 0) >= 30 and not behind
               else "moyenne" if (min_angle or 0) >= 20 and not behind else "faible")
    lat, lon = to_ll(ex, ey)
    return {"lat": lat, "lon": lon, "radius_km": radius, "min_angle": min_angle,
            "crossings": [to_ll(x, y) for (x, y), _ in crossings], "behind": behind, "quality": quality}
