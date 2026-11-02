#!/usr/bin/env python3
"""
Satellites : images Meteor-M (LRPT, 137,9 MHz), images SSTV de l'ISS
(145,800 MHz, événements ARISS) et relevé quotidien du bruit radio.

Ce service tourne en permanence (il ne tient pas la clé SDR) :
  - il calcule les passages au-dessus de la station (éléments CelesTrak, SGP4) ;
  - au lever d'un passage assez haut, il prépare la réception et donne la clé
    au module « meteor » (service meteor-rx) ; au coucher il rend la clé au
    module précédent ;
  - il range les images décodées (PNG + vignette) et les sert à l'appli.
La veille 121,5 MHz n'est jamais interrompue : le passage est alors ignoré.

Port METEOR_PORT (5013) :
  GET  /api/meteor/status          réglages, prochains passages, réception en cours
  GET  /api/meteor/images          images (plus récentes d'abord)
  GET  /api/meteor/image/<nom>     image complète (PNG)
  GET  /api/meteor/thumb/<nom>     vignette (JPEG)
  POST /api/meteor/config          {"enabled", "min_el", "gain", "sstv_enabled", "sstv_until",
                                    "sstv_min_el", "noise_enabled", "noise_time"}
  POST /api/meteor/delete/<nom>    supprime une image
  GET  /api/meteor/noise           historique des relevés de bruit
  POST /api/meteor/noise/run       relevé de bruit dès que possible
"""

import json
import logging
import math
import os
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from sgp4.api import Satrec, jday
from sgp4.propagation import gstime

PORT = int(os.environ.get("METEOR_PORT", "5013"))
DATA = os.environ.get("METEOR_DIR", "/var/lib/meteor")
IMG_DIR = os.path.join(DATA, "images")
CONF_FILE = os.path.join(DATA, "config.json")
TLE_FILE = os.path.join(DATA, "tle.json")
JOB_FILE = "/run/meteor/job.json"
SWITCH = "http://127.0.0.1:5006"
SATS = {57166: "Meteor-M N2-3", 59051: "Meteor-M N2-4", 25544: "ISS"}
ISS = 25544
NOISE_FILE = os.path.join(DATA, "noise.json")
NOISE_BANDS = [("Aviation 118-137", 118, 137), ("2 m 144-146", 144, 146), ("Marine 156-163", 156, 163),
               ("400-406", 400, 406), ("70 cm 430-440", 430, 440), ("ADS-B 1090", 1088, 1092)]
KEEP = 40

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("meteor")

conf = {"enabled": True, "min_el": 25, "gain": 40, "freq_mhz": 137.9, "symrate": 72000, "mode": "oqpsk",
        "diff": True, "lat": 43.590541, "lon": 7.002072, "alt_m": 200,
        "sstv_enabled": False, "sstv_until": "", "sstv_min_el": 15, "sstv_freq_mhz": 145.8,
        "noise_enabled": True, "noise_time": "03:30", "noise_gain": 30}
state = {"passes": [], "current": None, "last": None, "error": None, "tle_time": None,
         "noise_request": False, "last_noise_day": None}


def sstv_active():
    if conf.get("sstv_enabled"):
        return True
    until = conf.get("sstv_until") or ""
    return bool(until) and time.strftime("%Y-%m-%d") <= until

RE_KM = 6378.137
F_WGS = 1 / 298.257223563


# ------------------------------------------------------------ Orbites --

def load_conf():
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            conf.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_conf():
    os.makedirs(DATA, exist_ok=True)
    with open(CONF_FILE, "w", encoding="utf-8") as f:
        json.dump(conf, f)


def fetch_tles(force=False):
    try:
        with open(TLE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = None
    if cache and not force and time.time() - cache.get("time", 0) < 12 * 3600:
        state["tle_time"] = cache["time"]
        return cache["tles"]
    tles = {}
    for catnr in SATS:
        try:
            url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={catnr}&FORMAT=tle"
            req = urllib.request.Request(url, headers={"User-Agent": "STATION-SDR meteor (F4JCF)"})
            lines = [l.strip() for l in urllib.request.urlopen(req, timeout=30).read().decode().splitlines() if l.strip()]
            if len(lines) >= 3 and lines[1].startswith("1 ") and lines[2].startswith("2 "):
                tles[str(catnr)] = [lines[1], lines[2]]
        except Exception as exc:
            log.warning("Éléments orbitaux %s indisponibles : %s", catnr, exc)
    if tles:
        os.makedirs(DATA, exist_ok=True)
        with open(TLE_FILE, "w", encoding="utf-8") as f:
            json.dump({"time": time.time(), "tles": tles}, f)
        state["tle_time"] = time.time()
        return tles
    if cache:
        return cache["tles"]
    raise RuntimeError("éléments orbitaux indisponibles (CelesTrak injoignable)")


def observer():
    lat, lon = math.radians(conf["lat"]), math.radians(conf["lon"])
    e2 = F_WGS * (2 - F_WGS)
    n = RE_KM / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    h = conf["alt_m"] / 1000
    return lat, lon, ((n + h) * math.cos(lat) * math.cos(lon), (n + h) * math.cos(lat) * math.sin(lon),
                      (n * (1 - e2) + h) * math.sin(lat))


def look(rec, obs, t):
    tm = time.gmtime(t)
    jd, fr = jday(tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec + (t % 1))
    err, r, _v = rec.sgp4(jd, fr)
    if err:
        return None
    g = gstime(jd + fr)
    c, s = math.cos(g), math.sin(g)
    x, y, z = c * r[0] + s * r[1], -s * r[0] + c * r[1], r[2]
    lat, lon, o = obs
    dx, dy, dz = x - o[0], y - o[1], z - o[2]
    sl, cl, so, co = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    e = -so * dx + co * dy
    n = -sl * co * dx - sl * so * dy + cl * dz
    u = cl * co * dx + cl * so * dy + sl * dz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    sub_lat = math.degrees(math.atan2(z, math.hypot(x, y)))
    return (math.degrees(math.atan2(e, n)) + 360) % 360, math.degrees(math.asin(u / rng)), sub_lat


def compute_passes(hours=24):
    tles = fetch_tles()
    obs = observer()
    now = time.time()
    out = []
    for catnr, (l1, l2) in tles.items():
        rec = Satrec.twoline2rv(l1, l2)
        t, prev, cur = now - 900, None, None
        while t < now + hours * 3600:
            lk = look(rec, obs, t)
            if lk is None:
                break
            if prev is not None and prev[1] < 0 <= lk[1]:
                cur = {"sat": SATS.get(int(catnr), catnr), "catnr": int(catnr), "aos": t, "max_el": lk[1],
                       "kind": "sstv" if int(catnr) == ISS else "lrpt",
                       "max_t": t, "aos_lat": lk[2], "aos_az": lk[0]}
            if cur is not None and lk[1] > cur["max_el"]:
                cur.update(max_el=lk[1], max_t=t, max_az=lk[0])
            if cur is not None and prev is not None and prev[1] >= 0 > lk[1]:
                cur.update(los=t, los_lat=lk[2], los_az=lk[0])
                cur["northbound"] = cur["los_lat"] > cur["aos_lat"]
                cur["max_el"] = round(cur["max_el"], 1)
                if cur["los"] > now and cur["max_el"] >= 5:
                    out.append(cur)
                cur = None
            prev = lk
            t += 20
    out.sort(key=lambda p: p["aos"])
    return out


# ------------------------------------------------------- Clé SDR --

def sdr_mode():
    with urllib.request.urlopen(f"{SWITCH}/api/sdr/mode", timeout=10) as r:
        return json.loads(r.read().decode()).get("mode")


def sdr_switch(mode):
    req = urllib.request.Request(f"{SWITCH}/api/sdr/mode/{mode}?auto=1", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def run_job(job, info):
    """Donne la clé au module « meteor » pour la tâche `job`, attend la fin
    (et le décodage), puis rend la clé au module précédent."""
    try:
        prev = sdr_mode()
    except Exception as exc:
        state["error"] = f"bascule de clé injoignable : {exc}"
        return None
    if prev in ("elt121", "off"):
        log.info("Tâche %s ignorée : clé %s", job["name"], prev)
        state["last"] = {**info, "name": job["name"], "result": "ignoré (veille 121,5 ou clé arrêtée)"}
        return None
    os.makedirs(os.path.dirname(JOB_FILE), exist_ok=True)
    try:
        os.remove(JOB_FILE + ".done")
    except OSError:
        pass
    with open(JOB_FILE, "w", encoding="utf-8") as f:
        json.dump(job, f)
    state["current"] = {**info, "name": job["name"], "prev_mode": prev}
    log.info("Tâche %s : clé prise à %s", job["name"], prev)
    res = None
    try:
        sdr_switch("meteor")
        while time.time() < job["los"] and not os.path.exists(JOB_FILE + ".done"):
            time.sleep(5)
        # Laisse meteor-rx finir le décodage (il écrit .done)
        for _ in range(90):
            if os.path.exists(JOB_FILE + ".done"):
                break
            time.sleep(2)
    finally:
        try:
            if sdr_mode() == "meteor":
                sdr_switch(prev)
        except Exception as exc:
            log.warning("Retour de la clé à %s impossible : %s", prev, exc)
        try:
            with open(JOB_FILE + ".done", encoding="utf-8") as f:
                res = json.load(f)
            os.remove(JOB_FILE + ".done")
        except (OSError, ValueError):
            pass
        state["current"] = None
        state["last"] = {**info, "name": job["name"], "result": (res or {}).get("result", "pas de résultat")}
        prune()
    return res


def run_pass(p):
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(p["aos"]))
    if p.get("kind") == "sstv":
        name = f"{stamp}_ISS"
        job = {"kind": "sstv", "name": name, "sat": p["sat"], "los": p["los"] + 10,
               "freq_mhz": conf["sstv_freq_mhz"], "gain": conf["gain"], "out_dir": IMG_DIR,
               "max_el": p["max_el"], "aos": p["aos"]}
    else:
        name = f"{stamp}_{p['sat'].split()[-1]}"
        job = {"kind": "lrpt", "name": name, "sat": p["sat"], "los": p["los"] + 15, "freq_mhz": conf["freq_mhz"],
               "gain": conf["gain"], "symrate": conf["symrate"], "mode": conf["mode"], "diff": conf["diff"],
               "northbound": p.get("northbound"), "out_dir": IMG_DIR, "max_el": p["max_el"], "aos": p["aos"]}
    log.info("Passage %s (élévation max %.0f°)", p["sat"], p["max_el"])
    run_job(job, p)


def run_noise():
    job = {"kind": "noise", "name": time.strftime("bruit-%Y%m%d-%H%M"), "sat": "bruit", "los": time.time() + 600,
           "gain": conf["noise_gain"], "bands": NOISE_BANDS}
    run_job(job, {"sat": "Relevé du bruit", "kind": "noise", "aos": time.time()})
    state["last_noise_day"] = time.strftime("%Y-%m-%d")


def noise_due(now):
    if state["noise_request"]:
        return True
    if not conf.get("noise_enabled") or state["last_noise_day"] == time.strftime("%Y-%m-%d"):
        return False
    return time.strftime("%H:%M") >= (conf.get("noise_time") or "03:30")


def wanted(p):
    if p.get("kind") == "sstv":
        return sstv_active() and p["max_el"] >= conf["sstv_min_el"]
    return conf["enabled"] and p["max_el"] >= conf["min_el"]


def prune():
    try:
        pngs = sorted(f for f in os.listdir(IMG_DIR) if f.endswith(".png"))
    except OSError:
        return
    sstv = [f for f in pngs if "_ISS" in f]
    lrpt = [f for f in pngs if "_ISS" not in f]
    for old in lrpt[:-KEEP] + sstv[:-KEEP]:
        for ext in (".png", ".jpg", ".json"):
            try:
                os.remove(os.path.join(IMG_DIR, old[:-4] + ext))
            except OSError:
                pass


def noise_history():
    try:
        with open(NOISE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def scheduler():
    last_calc = 0
    # Redémarrage pendant une réception : rendre la clé
    try:
        if sdr_mode() == "meteor":
            sdr_switch("adsb")
    except Exception:
        pass
    while True:
        try:
            if time.time() - last_calc > 3600 or not state["passes"]:
                state["passes"] = compute_passes()
                last_calc = time.time()
                state["error"] = None
            now = time.time()
            for p in state["passes"]:
                if wanted(p) and p["aos"] - 30 <= now < p["los"] - 60:
                    run_pass(p)
                    last_calc = 0
                    break
            else:
                # Relevé du bruit : seulement si aucun passage voulu dans les 15 min
                if noise_due(now) and not any(wanted(p) and p["aos"] - now < 900 and p["los"] > now
                                              for p in state["passes"]):
                    state["noise_request"] = False
                    run_noise()
        except Exception as exc:
            state["error"] = str(exc)
            log.exception("Planification")
        time.sleep(15)


# --------------------------------------------------------------- HTTP --

def images():
    out = []
    try:
        for f in sorted(os.listdir(IMG_DIR), reverse=True):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(IMG_DIR, f), encoding="utf-8") as fh:
                        out.append(json.load(fh))
                except (OSError, ValueError):
                    pass
    except OSError:
        pass
    return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/meteor/status":
            passes = [{**p, "wanted": wanted(p)} for p in state["passes"]]
            self._send(200, {**state, "config": conf, "passes": passes[:16], "sstv_active": sstv_active(),
                             "noise_last": (noise_history() or [None])[-1]})
        elif path == "/api/meteor/noise":
            self._send(200, {"bands": [b[0] for b in NOISE_BANDS], "history": noise_history()})
        elif path == "/api/meteor/images":
            self._send(200, images())
        elif path.startswith(("/api/meteor/image/", "/api/meteor/thumb/")):
            name = path.rsplit("/", 1)[-1]
            if not re.fullmatch(r"[\w.-]+", name):
                self._send(404, {"error": "introuvable"})
                return
            ext, ctype = (".png", "image/png") if "/image/" in path else (".jpg", "image/jpeg")
            try:
                with open(os.path.join(IMG_DIR, name + ext), "rb") as f:
                    self._send(200, f.read(), ctype)
            except OSError:
                self._send(404, {"error": "introuvable"})
        else:
            self._send(404, {"error": "introuvable"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/meteor/config":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if "enabled" in body:
                    conf["enabled"] = bool(body["enabled"])
                if "min_el" in body:
                    conf["min_el"] = max(5, min(80, float(body["min_el"])))
                if "gain" in body:
                    conf["gain"] = max(0.0, min(49.6, float(body["gain"])))
                for key in ("sstv_enabled", "noise_enabled"):
                    if key in body:
                        conf[key] = bool(body[key])
                if "sstv_min_el" in body:
                    conf["sstv_min_el"] = max(5, min(80, float(body["sstv_min_el"])))
                if "sstv_until" in body:
                    v = str(body["sstv_until"] or "").strip()
                    if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                        raise ValueError("date attendue au format AAAA-MM-JJ")
                    conf["sstv_until"] = v
                if "noise_time" in body:
                    v = str(body["noise_time"]).strip()
                    if not re.fullmatch(r"[0-2]\d:[0-5]\d", v):
                        raise ValueError("heure attendue au format HH:MM")
                    conf["noise_time"] = v
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": str(exc)})
                return
            save_conf()
            self._send(200, {"ok": True, "config": conf})
        elif path == "/api/meteor/noise/run":
            state["noise_request"] = True
            self._send(200, {"ok": True, "message": "relevé programmé (dans la minute, hors passage)"})
        elif path.startswith("/api/meteor/delete/"):
            name = path.rsplit("/", 1)[-1]
            if re.fullmatch(r"[\w.-]+", name):
                for ext in (".png", ".jpg", ".json"):
                    try:
                        os.remove(os.path.join(IMG_DIR, name + ext))
                    except OSError:
                        pass
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "introuvable"})

    def log_message(self, *_a):
        pass


if __name__ == "__main__":
    load_conf()
    os.makedirs(IMG_DIR, exist_ok=True)
    hist = noise_history()
    if hist:
        state["last_noise_day"] = time.strftime("%Y-%m-%d", time.localtime(hist[-1]["time"]))
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
