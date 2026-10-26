#!/usr/bin/env python3
"""
Surveillance de F4JCF-SDR-STATION : statistiques + notifications

Tourne en permanence sur le Pi (même PC éteint) :
  * relève toutes les minutes l'activité (avions, navires, stations APRS,
    température, sous-tension, module qui tient la clé) dans une base
    SQLite, et la portée maximale par direction (polaires ADS-B et AIS) ;
  * détecte les événements de sécurité (squawk 7700/7600/7500, balises
    SART/EPIRB/MOB, messages APRS pour l'indicatif, courrier Winlink,
    sous-tension, clé SDR disparue) et les envoie sur le téléphone (ntfy)
    et/ou par e-mail.

Endpoints (port STATION_MONITOR_PORT, 5008) :
  GET  /api/stats/series?hours=24
  GET  /api/stats/polar?kind=adsb|ais&hours=24
  GET  /api/monitor/config          (sans le mot de passe)
  POST /api/monitor/config
  POST /api/monitor/test
  GET  /api/monitor/notifications

Bibliothèque standard uniquement.
"""

import json
import logging
import math
import os
import smtplib
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("STATION_MONITOR_PORT", "5008"))
CONFIG_PATH = os.environ.get("STATION_MONITOR_CONFIG", "/etc/station-monitor.json")
DB_PATH = os.environ.get("STATION_MONITOR_DB", "/var/lib/station-monitor/stats.db")
SAMPLE_SECONDS = 60
KEEP_DAYS = 30

DEFAULT_CONFIG = {
    "mycall": "F4JCF",
    "station_lat": 43.590541,
    "station_lon": 7.002072,
    "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": ""},
    "email": {"enabled": False, "smtp_host": "mail.infomaniak.com", "smtp_port": 465, "ssl": True,
              "user": "", "password": "", "to": ""},
    "events": {"squawk": True, "sart": True, "aprs_msg": True, "winlink": True,
               "undervoltage": True, "dongle": True, "vigilance": True, "beacon": True, "beacon_test": True,
               "elt121": True},
    # Vigilance Météo-France : clé gratuite à créer sur portail-api.meteofrance.fr
    # (API « Données Publiques Vigilance »).
    "vigilance": {"api_key": "", "dept": "06"},
    "daily_report": {"enabled": False, "hour": 20},
}
EVENT_LABELS = {
    "squawk": "Squawk d'urgence avion (7700 / 7600 / 7500)",
    "sart": "Balise de détresse AIS (SART / EPIRB / MOB)",
    "aprs_msg": "Message APRS pour mon indicatif",
    "winlink": "Nouveau courrier Winlink",
    "undervoltage": "Sous-tension du Pi",
    "dongle": "Clé SDR disparue de l'USB",
    "vigilance": "Vigilance Météo-France orange ou rouge",
    "beacon": "Balise de détresse 406 MHz (émission réelle)",
    "beacon_test": "Balise 406 MHz en mode test / exercice",
    "elt121": "Sirène de balise sur 121,5 MHz",
}
VIGI_URL = "https://public-api.meteofrance.fr/public/DPVigilance/v1/cartevigilance/encours"
VIGI_PHENOMENA = {
    "1": "Vent violent", "2": "Pluie-inondation", "3": "Orages", "4": "Crues", "5": "Neige-verglas",
    "6": "Canicule", "7": "Grand froid", "8": "Avalanches", "9": "Vagues-submersion",
}
VIGI_COLORS = {1: "vert", 2: "jaune", 3: "orange", 4: "rouge"}
_vigilance = {"status": "non configurée", "fetched_at": None, "periods": []}
SQUAWKS = {"7700": "Détresse (urgence générale)", "7600": "Panne radio", "7500": "Intrusion illicite"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("station-monitor")
_lock = threading.Lock()
_sent = []                 # dernières notifications envoyées
_seen = {}                 # clé d'événement -> horodatage
_primed = False            # premier passage : on mémorise sans notifier


# ---------------------------------------------------------- Configuration --

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)  # contient le mot de passe SMTP
    os.replace(tmp, CONFIG_PATH)


def public_config(cfg: dict) -> dict:
    out = json.loads(json.dumps(cfg))
    out["email"]["password_set"] = bool(cfg["email"].get("password"))
    out["email"].pop("password", None)
    out["vigilance"]["api_key_set"] = bool(cfg["vigilance"].get("api_key"))
    out["vigilance"].pop("api_key", None)
    out["event_labels"] = EVENT_LABELS
    return out


def update_config(data: dict) -> dict:
    cfg = load_config()
    for section in ("ntfy", "email", "events", "vigilance", "daily_report"):
        if isinstance(data.get(section), dict):
            for k, v in data[section].items():
                if (section, k) in (("email", "password"), ("vigilance", "api_key")) and not v:
                    continue  # champ vide = garder le secret actuel
                if k in cfg[section]:
                    cfg[section][k] = v
    for k in ("mycall",):
        if isinstance(data.get(k), str) and data[k].strip():
            cfg[k] = data[k].strip().upper()
    save_config(cfg)
    return cfg


# ----------------------------------------------------------- Envoi --------

def send_ntfy(cfg: dict, title: str, message: str, priority: int) -> None:
    n = cfg["ntfy"]
    if not n.get("topic"):
        raise ValueError("aucun sujet ntfy configuré")
    payload = {
        "topic": n["topic"], "title": title, "message": message, "priority": priority,
        "tags": ["rotating_light"] if priority >= 5 else ["warning"] if priority >= 4 else ["satellite"],
    }
    ip = lan_ip()
    if ip:
        payload["click"] = f"http://{ip}:{PORT}/"  # un appui ouvre le tableau de bord (à la maison)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(n.get("server", "https://ntfy.sh").rstrip("/") + "/", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def lan_ip():
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.168.1.254", 9))  # aucun paquet envoyé : sert à choisir l'interface
            return sk.getsockname()[0]
    except OSError:
        return None


def send_email(cfg: dict, title: str, message: str) -> None:
    e = cfg["email"]
    if not (e.get("smtp_host") and e.get("user") and e.get("password") and e.get("to")):
        raise ValueError("e-mail incomplet (serveur, identifiant, mot de passe, destinataire)")
    msg = EmailMessage()
    msg["Subject"] = f"[F4JCF-SDR-STATION] {title}"
    msg["From"] = e["user"]
    msg["To"] = e["to"]
    msg.set_content(message + "\n\n-- \nNotification automatique de F4JCF-SDR-STATION")
    port = int(e.get("smtp_port") or 465)
    if e.get("ssl", True):
        with smtplib.SMTP_SSL(e["smtp_host"], port, timeout=20) as s:
            s.login(e["user"], e["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(e["smtp_host"], port, timeout=20) as s:
            s.starttls()
            s.login(e["user"], e["password"])
            s.send_message(msg)


def notify(title: str, message: str, priority: int = 4, event: str = None, force: bool = False) -> dict:
    cfg = load_config()
    if event and not force and not cfg["events"].get(event, True):
        return {"skipped": True}
    result = {"time": time.time(), "title": title, "message": message, "channels": {}}
    if cfg["ntfy"].get("enabled") or force:
        if cfg["ntfy"].get("topic"):
            try:
                send_ntfy(cfg, title, message, priority)
                result["channels"]["ntfy"] = "ok"
            except Exception as exc:
                result["channels"]["ntfy"] = f"échec : {exc}"
    if cfg["email"].get("enabled") or (force and cfg["email"].get("password")):
        try:
            send_email(cfg, title, message)
            result["channels"]["email"] = "ok"
        except Exception as exc:
            result["channels"]["email"] = f"échec : {exc}"
    log.info("Notification « %s » : %s", title, result["channels"] or "aucun canal actif")
    with _lock:
        _sent.insert(0, result)
        del _sent[50:]
    return result


def event_once(key: str, cooldown: float) -> bool:
    """Vrai si l'événement n'a pas été signalé depuis `cooldown` secondes.
    Au tout premier passage, on mémorise sans signaler (évite une rafale
    de notifications pour des événements anciens au démarrage)."""
    now = time.time()
    last = _seen.get(key)
    _seen[key] = now
    if not _primed:
        return False
    return last is None or now - last >= cooldown


# --------------------------------------------------------- Collecte -------

def fetch_json(url: str, timeout: float = 5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def read_aircraft():
    for path in ("/run/readsb/aircraft.json", "/run/dump1090-fa/aircraft.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return fetch_json("http://127.0.0.1:8504/data/aircraft.json")


def distance_bearing(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    dist = 2 * r * math.asin(math.sqrt(a))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return dist, (math.degrees(math.atan2(y, x)) + 360) % 360


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS samples (
        t INTEGER PRIMARY KEY, aircraft INTEGER, aircraft_pos INTEGER, adsb_max_km REAL,
        ships INTEGER, ais_max_km REAL, aprs_stations INTEGER, temp REAL, undervolt INTEGER,
        mode TEXT, dongles INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS polar (
        kind TEXT, hour INTEGER, sector INTEGER, max_km REAL, PRIMARY KEY (kind, hour, sector))""")
    return con


def record_polar(con, kind, hour, points, lat0, lon0):
    best = {}
    for lat, lon in points:
        dist, brg = distance_bearing(lat0, lon0, lat, lon)
        if dist > 800:  # position aberrante
            continue
        sector = int(brg // 10) % 36
        best[sector] = max(best.get(sector, 0), dist)
    for sector, dist in best.items():
        con.execute(
            "INSERT INTO polar(kind, hour, sector, max_km) VALUES (?,?,?,?) "
            "ON CONFLICT(kind, hour, sector) DO UPDATE SET max_km = MAX(max_km, excluded.max_km)",
            (kind, hour, sector, round(dist, 1)),
        )
    return round(max(best.values()), 1) if best else None


def collect_once(state: dict) -> None:
    global _primed
    cfg = load_config()
    lat0, lon0 = float(cfg["station_lat"]), float(cfg["station_lon"])
    now = int(time.time())
    hour = now - now % 3600
    mode_info = fetch_json("http://127.0.0.1:5006/api/sdr/mode") or {}
    mode = mode_info.get("mode")
    health = fetch_json("http://127.0.0.1:5006/api/system/health") or {}

    # --- ADS-B
    ac_data = read_aircraft() if mode == "adsb" else None
    aircraft = (ac_data or {}).get("aircraft", []) if ac_data else []
    # On n'utilise que les avions vus récemment (moins de 60 s).
    aircraft = [a for a in aircraft if (a.get("seen") or 0) < 60]
    ac_pos = [(a["lat"], a["lon"]) for a in aircraft
              if isinstance(a.get("lat"), (int, float)) and isinstance(a.get("lon"), (int, float))
              and (a.get("seen_pos") or 0) < 60]

    # --- AIS
    ais = fetch_json("http://127.0.0.1:8100/api/ships.json") if mode == "ais" else None
    ships = (ais or {}).get("ships", []) if isinstance(ais, dict) else []
    ship_pos = [(s["lat"], s["lon"]) for s in ships
                if isinstance(s.get("lat"), (int, float)) and isinstance(s.get("lon"), (int, float))
                and (s.get("last_signal") or 0) < 600]

    # --- APRS
    stations = fetch_json("http://127.0.0.1:5004/api/aprs/stations") or []
    aprs_recent = sum(1 for s in stations if now - (s.get("last_heard") or 0) < 1800) if isinstance(stations, list) else None

    th = health.get("throttled") or {}
    con = db()
    try:
        adsb_max = record_polar(con, "adsb", hour, ac_pos, lat0, lon0) if mode == "adsb" else None
        ais_max = record_polar(con, "ais", hour, ship_pos, lat0, lon0) if mode == "ais" else None
        con.execute(
            "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now, len(aircraft) if mode == "adsb" else None, len(ac_pos) if mode == "adsb" else None, adsb_max,
             len(ships) if mode == "ais" else None, ais_max, aprs_recent if mode == "aprs" else None,
             health.get("cpu_temp_c"), 1 if th.get("under_voltage_now") else 0, mode, health.get("sdr_dongles")),
        )
        if now - state.get("last_purge", 0) > 3600:
            limit = now - KEEP_DAYS * 86400
            con.execute("DELETE FROM samples WHERE t < ?", (limit,))
            con.execute("DELETE FROM polar WHERE hour < ?", (limit,))
            state["last_purge"] = now
        con.commit()
    finally:
        con.close()

    # --- Événements
    for a in aircraft:
        sq = str(a.get("squawk") or "")
        if sq in SQUAWKS and event_once(f"sq:{a.get('hex')}:{sq}", 1800):
            call = (a.get("flight") or "").strip() or str(a.get("hex", "?")).upper()
            pos = f"\nPosition : {a['lat']:.4f}, {a['lon']:.4f}" if isinstance(a.get("lat"), (int, float)) else ""
            notify(f"Squawk {sq} — {call}",
                   f"{call} (hex {a.get('hex')}) affiche {sq} : {SQUAWKS[sq]}.\n"
                   f"Altitude : {a.get('alt_baro', '—')} ft{pos}", 5, "squawk")
    for s in ships:
        try:
            m = int(s.get("mmsi"))
        except (TypeError, ValueError):
            continue
        kind = ("Balise SART" if 970000000 <= m <= 970999999 or s.get("shipclass") == 13 else
                "Balise homme à la mer" if 972000000 <= m <= 972999999 else
                "Balise EPIRB" if 974000000 <= m <= 974999999 else None)
        if kind and event_once(f"ais:{m}", 3600):
            pos = f"\nPosition : {s['lat']:.4f}, {s['lon']:.4f}" if isinstance(s.get("lat"), (int, float)) else ""
            notify(f"AIS — {kind}", f"Émission de détresse reçue : MMSI {m}.{pos}\n"
                   "Peut être un essai : vérifier avant d'alerter.", 5, "sart")

    packets = fetch_json("http://127.0.0.1:5004/api/aprs/packets") or []
    mycall = str(cfg.get("mycall", "F4JCF")).upper()
    for p in packets if isinstance(packets, list) else []:
        raw = str(p.get("raw") or "")
        if len(raw) < 11 or raw[0] != ":" or raw[10] != ":":
            continue
        addressee = raw[1:10].strip().upper()
        body = raw[11:]
        if addressee.split("-")[0] != mycall or body.startswith(("ack", "rej")):
            continue
        text, _, mid = body.partition("{")
        if event_once(f"aprs:{p.get('callsign')}:{mid or text}", 86400):
            notify(f"Message APRS de {p.get('callsign')}", f"Pour {addressee} : {text.strip()}", 4, "aprs_msg")

    inbox = fetch_json("http://127.0.0.1:8080/api/mailbox/in") or []
    for m in inbox if isinstance(inbox, list) else []:
        mid = m.get("MID")
        if mid and event_once(f"wl:{mid}", 10 ** 9):
            frm = m.get("From")
            frm = frm.get("Addr", "") if isinstance(frm, dict) else str(frm or "")
            notify(f"Winlink — message de {frm}", f"Objet : {m.get('Subject') or '(sans objet)'}", 3, "winlink")

    beacons = fetch_json("http://127.0.0.1:5009/api/beacon/frames?limit=20") or []
    for f in beacons if isinstance(beacons, list) else []:
        if f.get("manual") or time.time() - (f.get("time") or 0) > 3600:
            continue
        test = bool(f.get("test"))
        if not event_once(f"b406:{f.get('hex_id')}:{test}", 3600 if test else 600):
            continue
        pos = ""
        if f.get("lat") is not None:
            pos = (f"\nPosition : {f['lat']:.5f}, {f['lon']:.5f}  ({f.get('distance_km')} km, "
                   f"relèvement {f.get('bearing_deg')}°)")
        body = (f"Hex ID {f.get('hex_id')} · {f.get('protocol') or '?'} · {f.get('country') or '?'}\n"
                f"{f.get('identification') or ''}{pos}"
                + (f"\n{f.get('freq_mhz')} MHz, SNR {f.get('snr_db')} dB" if f.get("freq_mhz") else ""))
        if test:
            notify("Balise 406 — trame de TEST / exercice", body, 3, "beacon_test")
        else:
            notify("BALISE 406 — ÉMISSION RÉELLE", body + "\nVérifier : alerte possible en cours (CROSS / ARCC).",
                   5, "beacon")

    elt = fetch_json("http://127.0.0.1:5011/api/elt121/events") or []
    for e in elt[:1] if isinstance(elt, list) else []:
        if time.time() - (e.get("start") or 0) < 600 and event_once("elt121", 900):
            exo = abs(float(e.get("freq_mhz") or 0) - 121.475) < 0.001
            notify(("Sirène d'EXERCICE" if exo else "Sirène de balise") + f" sur {e.get('freq_mhz')} MHz",
                   f"Détectée à {time.strftime('%H:%M:%S', time.localtime(e['start']))} sur "
                   f"{e.get('freq_mhz')} MHz, {e.get('max_above_db')} dB au-dessus du bruit, "
                   f"{e.get('sweeps_per_s')} balayages/s.\nEnregistrement disponible dans l'appli "
                   "(Balises de détresse → 121,5 MHz).", 4, "elt121")

    if th.get("under_voltage_now") and event_once("undervolt", 6 * 3600):
        notify("Pi en sous-tension", "Le Raspberry Pi manque de courant : la clé SDR risque de décrocher.\n"
               "Remède : hub USB alimenté et/ou alimentation 5,1 V / 2,5 A.", 4, "undervoltage")
    dongles = health.get("sdr_dongles")
    if dongles == 0:
        state["no_dongle"] = state.get("no_dongle", 0) + 1
        if state["no_dongle"] >= 2 and event_once("nodongle", 6 * 3600):
            notify("Clé SDR disparue", "Aucune clé RTL-SDR n'est visible sur l'USB du Pi depuis 2 minutes.\n"
                   "Débrancher / rebrancher la clé, et vérifier l'alimentation.", 4, "dongle")
    elif dongles:
        state["no_dongle"] = 0
    _primed = True


def parse_vigilance(data: dict, dept: str) -> list:
    """Extrait, pour le département, la couleur maximale et les phénomènes
    de chaque échéance (J = aujourd'hui, J1 = demain)."""
    periods = []
    for per in ((data or {}).get("product") or {}).get("periods") or []:
        domains = (per.get("timelaps") or {}).get("domain_ids") or []
        dom = next((d for d in domains if str(d.get("domain_id")) == dept), None)
        if dom is None:
            continue
        phen = []
        for item in dom.get("phenomenon_items") or []:
            color = int(item.get("phenomenon_max_color_id") or 1)
            pid = str(item.get("phenomenon_id"))
            if color >= 2:
                phen.append({"id": pid, "name": VIGI_PHENOMENA.get(pid, f"Phénomène {pid}"),
                             "color": color, "color_name": VIGI_COLORS.get(color, "?")})
        max_color = int(dom.get("max_color_id") or 1)
        periods.append({
            "echeance": per.get("echeance"), "begin": per.get("begin_validity_time"),
            "end": per.get("end_validity_time"), "max_color": max_color,
            "max_color_name": VIGI_COLORS.get(max_color, "?"),
            "phenomena": sorted(phen, key=lambda p: -p["color"]),
        })
    return periods


def fetch_vigilance(key: str) -> dict:
    """Le portail Météo-France délivre deux sortes d'identifiants : une
    « API Key » (en-tête apikey) ou un jeton OAuth2 (en-tête Authorization:
    Bearer, valable 1 h). On essaie les deux pour éviter qu'une clé correcte
    soit refusée parce qu'elle n'est pas envoyée sous la bonne forme."""
    last = None
    for headers in ({"apikey": key}, {"Authorization": f"Bearer {key}"}):
        headers["accept"] = "*/*"
        req = urllib.request.Request(VIGI_URL, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
                msg = json.loads(body)
                exc.detail = (msg.get("description") or msg.get("message") or msg.get("fault", {}).get("message")
                              or body)[:200] if isinstance(msg, dict) else body[:200]
            except Exception:
                exc.detail = ""
            last = exc
            if exc.code not in (401, 403):
                raise
    raise last


def vigilance_loop() -> None:
    while True:
        cfg = load_config()
        key, dept = cfg["vigilance"].get("api_key"), str(cfg["vigilance"].get("dept") or "06")
        if not key:
            _vigilance.update(status="non configurée", periods=[])
            time.sleep(60)
            continue
        try:
            data = fetch_vigilance(key.strip())
            periods = parse_vigilance(data, dept)
            _vigilance.update(status="ok", fetched_at=time.time(), periods=periods, dept=dept)
            for per in periods:
                if per["max_color"] >= 3:
                    phen = ", ".join(f"{p['name']} ({p['color_name']})" for p in per["phenomena"] if p["color"] >= 3)
                    day = "aujourd'hui" if per["echeance"] == "J" else "demain"
                    key_ev = f"vigi:{per['echeance']}:{per.get('begin')}:{per['max_color']}:{phen}"
                    if event_once(key_ev, 12 * 3600):
                        notify(f"Vigilance {per['max_color_name'].upper()} — dép. {dept}",
                               f"Météo-France place le département {dept} en vigilance {per['max_color_name']} "
                               f"{day} : {phen}.", 5 if per["max_color"] == 4 else 4, "vigilance")
        except urllib.error.HTTPError as exc:
            detail = getattr(exc, "detail", "")
            _vigilance.update(status=f"erreur {exc.code}" + (" : clé API refusée" if exc.code in (401, 403) else "")
                              + (f" — Météo-France : {detail}" if detail else ""))
        except Exception as exc:
            _vigilance.update(status=f"erreur : {exc}")
        # Attente : 30 min si tout va bien, 5 min après une erreur — mais on
        # réessaie tout de suite si une nouvelle clé est enregistrée.
        wait = 1800 if _vigilance.get("status") == "ok" else 300
        for _ in range(wait // 5):
            time.sleep(5)
            if (load_config()["vigilance"].get("api_key") or "") != key:
                _vigilance.update(status="nouvelle clé : vérification en cours…")
                break


# ------------------------------------------------------ Rapport quotidien --

REPORT_STATE = os.path.join(os.path.dirname(DB_PATH), "report_last")
MODE_NAMES = {"adsb": "ADS-B", "ais": "AIS", "aprs": "APRS", "iss": "APRS ISS", "beacon": "Balise 406",
              "433": "433 MHz", "elt121": "Balise 121,5", "openwebrx": "OpenWebRX", "off": "arrêtée"}


def build_report(hours: int = 24) -> tuple:
    since = int(time.time() - hours * 3600)
    con = db()
    try:
        rows = con.execute("SELECT * FROM samples WHERE t >= ? ORDER BY t", (since,)).fetchall()
    finally:
        con.close()
    def col(i):
        return [r[i] for r in rows if r[i] is not None]
    lines = []
    n = len(rows) or 1
    ac, rng = col(1), col(3)
    if ac:
        lines.append(f"✈ ADS-B : jusqu'à {max(ac)} avions en même temps, portée max {max(rng):.0f} km" if rng
                     else f"✈ ADS-B : jusqu'à {max(ac)} avions en même temps")
    ships, srng = col(4), col(5)
    if ships:
        lines.append(f"⚓ AIS : jusqu'à {max(ships)} navires, portée max {max(srng):.0f} km" if srng
                     else f"⚓ AIS : jusqu'à {max(ships)} navires")
    aprs = col(6)
    if aprs:
        lines.append(f"📡 APRS VHF : jusqu'à {max(aprs)} stations sur 30 min")
    frames = fetch_json("http://127.0.0.1:5009/api/beacon/frames?limit=500")
    if isinstance(frames, list):
        recent = [f for f in frames if not f.get("manual") and (f.get("time") or 0) >= since]
        if recent:
            real = len({f.get("hex_id") for f in recent if not f.get("test")})
            test = len({f.get("hex_id") for f in recent if f.get("test")})
            lines.append(f"🆘 Balises 406 : {real} réelle(s), {test} en test")
        else:
            lines.append("🆘 Balises 406 : aucune")
    sensors = fetch_json("http://127.0.0.1:5010/api/433/devices")
    if isinstance(sensors, list) and sensors:
        act = sum(1 for d in sensors if (d.get("last_seen") or 0) >= since)
        lines.append(f"🌡 Capteurs 433 MHz : {act} actif(s)")
    elt = fetch_json("http://127.0.0.1:5011/api/elt121/events")
    if isinstance(elt, list) and elt:
        n = sum(1 for e in elt if (e.get("start") or 0) >= since)
        if n:
            lines.append(f"🆘 Sirène 121,5 MHz : {n} détection(s)")
    temps = col(7)
    uv = sum(1 for r in rows if r[8])
    if temps:
        lines.append(f"🖥 Pi : {min(temps):.0f}–{max(temps):.0f} °C"
                     + (f", sous-tension {uv} min" if uv else ", alimentation correcte"))
    nod = sum(1 for r in rows if r[10] == 0)
    if nod:
        lines.append(f"⚠ Clé SDR absente pendant {nod} min")
    modes = {}
    for r in rows:
        modes[r[9] or "?"] = modes.get(r[9] or "?", 0) + 1
    if rows:
        lines.append("🔑 Clé : " + ", ".join(f"{MODE_NAMES.get(m, m)} {c * 100 // n} %"
                                           for m, c in sorted(modes.items(), key=lambda x: -x[1])))
    with _lock:
        alerts = [a for a in _sent if a["time"] >= since and a["title"] != "Test de notification"]
    lines.append(f"🔔 Alertes envoyées : {len(alerts)}" + (
        " (" + ", ".join(a["title"] for a in alerts[:3]) + ("…" if len(alerts) > 3 else "") + ")" if alerts else ""))
    if _vigilance.get("status") == "ok" and _vigilance.get("periods"):
        worst = max(p.get("max_color", 1) for p in _vigilance["periods"])
        lines.append(f"☔ Vigilance 06 : {VIGI_COLORS.get(worst, '?')}")
    title = f"Rapport du {time.strftime('%d/%m/%Y')} — F4JCF-SDR-STATION"
    return title, "\n".join(lines) if lines else "Pas encore de données."


def report_loop() -> None:
    while True:
        try:
            cfg = load_config()
            dr = cfg.get("daily_report") or {}
            lt = time.localtime()
            today = time.strftime("%Y-%m-%d", lt)
            try:
                last = open(REPORT_STATE, encoding="utf-8").read().strip()
            except OSError:
                last = ""
            if dr.get("enabled") and lt.tm_hour == int(dr.get("hour", 20)) and last != today:
                title, body = build_report()
                notify(title, body, 2)
                with open(REPORT_STATE, "w", encoding="utf-8") as f:
                    f.write(today)
        except Exception:
            log.exception("Rapport quotidien")
        time.sleep(60)


def collector_loop() -> None:
    state = {}
    while True:
        start = time.time()
        try:
            collect_once(state)
        except Exception:
            log.exception("Erreur de collecte")
        time.sleep(max(5, SAMPLE_SECONDS - (time.time() - start)))


# ------------------------------------------------------------ Lecture -----

def series(hours: float) -> dict:
    hours = max(1, min(24 * KEEP_DAYS, hours))
    since = int(time.time() - hours * 3600)
    con = db()
    try:
        rows = con.execute("SELECT * FROM samples WHERE t >= ? ORDER BY t", (since,)).fetchall()
    finally:
        con.close()
    cols = ["t", "aircraft", "aircraft_pos", "adsb_max_km", "ships", "ais_max_km", "aprs_stations",
            "temp", "undervolt", "mode", "dongles"]
    # Au-delà de 48 h, on regroupe par tranches de 15 min (moins de points à tracer).
    bucket = 900 if hours > 48 else 0
    if bucket:
        grouped = {}
        for r in rows:
            grouped.setdefault(r[0] - r[0] % bucket, []).append(r)
        merged = []
        for t, rs in sorted(grouped.items()):
            def agg(i, fn=max):
                vals = [x[i] for x in rs if x[i] is not None]
                return fn(vals) if vals else None
            merged.append((t, agg(1), agg(2), agg(3), agg(4), agg(5), agg(6),
                           agg(7), agg(8), rs[-1][9], agg(10, min)))
        rows = merged
    return {"columns": cols, "rows": rows, "hours": hours}


def polar(kind: str, hours: float) -> dict:
    hours = max(1, min(24 * KEEP_DAYS, hours))
    since = int(time.time() - hours * 3600)
    con = db()
    try:
        rows = con.execute("SELECT sector, MAX(max_km) FROM polar WHERE kind = ? AND hour >= ? GROUP BY sector",
                           (kind, since - since % 3600)).fetchall()
    finally:
        con.close()
    sectors = [None] * 36
    for sector, dist in rows:
        sectors[int(sector)] = dist
    return {"kind": kind, "hours": hours, "sector_deg": 10, "max_km": sectors}


DASH_HTML = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0b0f17">
<title>F4JCF-SDR-STATION</title>
<style>
:root{--bg:#0b0f17;--card:#141b27;--alt:#1b2433;--border:#23304a;--text:#e6edf3;--muted:#8b96a5;
--accent:#ff7a1a;--green:#3fb950;--red:#f85149;--orange:#d29922;--yellow:#eab308}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:12px 12px 40px}
h1{font-size:19px;margin:4px 0 2px}.sub{color:var(--muted);font-size:12px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px;margin-bottom:12px}
.lbl{color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}
.big{font-size:22px;font-weight:700;margin-top:2px}.row{display:flex;gap:10px;flex-wrap:wrap}
.tile{flex:1 1 140px;background:var(--alt);border-radius:10px;padding:10px}
.green{color:var(--green)}.red{color:var(--red)}.orange{color:var(--orange)}.muted{color:var(--muted)}
.btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
button{font:inherit;font-weight:700;border:0;border-radius:9px;padding:11px 14px;background:var(--alt);color:var(--text)}
button.on{background:var(--accent);color:#1a1200}button.stop{background:var(--red);color:#0b0f17}
button:disabled{opacity:.5}
.pill{display:inline-block;border-radius:6px;padding:2px 8px;font-size:12px;font-weight:700;color:#0b0f17}
.alert{border-left:4px solid var(--red);padding:6px 10px;margin:6px 0;background:var(--alt);border-radius:6px}
.alert .t{font-size:11px;color:var(--muted)}.err{color:var(--red);font-size:13px}
</style></head><body>
<h1>F4JCF-SDR-STATION</h1><div class="sub" id="upd">Chargement…</div>
<div class="card"><div class="lbl">Clé SDR</div><div class="big" id="mode">—</div>
<div class="muted" id="modeinfo"></div><div class="btns" id="btns"></div><div class="err" id="btnerr"></div></div>
<div class="card"><div class="lbl">Activité</div><div class="row" id="act"></div></div>
<div class="card"><div class="lbl">Santé du Pi</div><div class="row" id="health"></div></div>
<div class="card"><div class="lbl">Vigilance Météo-France</div><div id="vigi" class="muted">—</div></div>
<div class="card"><div class="lbl">Dernières alertes</div><div id="alerts" class="muted">—</div></div>
<script>
const L={adsb:"ADS-B",ais:"AIS",aprs:"APRS",openwebrx:"OpenWebRX",beacon:"Balise 406",elt121:"121,5 MHz","433":"433 MHz",iss:"APRS ISS",off:"Arrêtée"};
const VC={1:["#3fb950","VERT"],2:["#eab308","JAUNE"],3:["#f97316","ORANGE"],4:["#f85149","ROUGE"]};
let busy=false;
function tile(l,v,c,s){return `<div class="tile"><div class="lbl">${l}</div><div class="big ${c||""}">${v}</div>${s?`<div class="muted" style="font-size:12px">${s}</div>`:""}</div>`}
function esc(t){return String(t??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
async function setMode(m){
  if(busy)return;
  if(m==="off"&&!confirm("Arrêter tous les modules qui utilisent la clé SDR ?"))return;
  busy=true;document.getElementById("btnerr").textContent="Bascule en cours…";render(window._d);
  try{const r=await fetch("/api/dash/sdr/"+m,{method:"POST"});if(!r.ok)throw new Error("erreur "+r.status);
      document.getElementById("btnerr").textContent=""}
  catch(e){document.getElementById("btnerr").textContent="Échec : "+e.message}
  busy=false;load()}
function render(d){
  if(!d)return;window._d=d;const m=d.mode||{};const h=d.health||{};const th=h.throttled||{};
  document.getElementById("mode").innerHTML=m.mode==="off"?'<span class="red">Arrêtée</span>':`<span style="color:var(--accent)">${L[m.mode]||"—"}</span>`;
  const sched={manual:"manuel",rotation:"rotation",planning:"planning horaire"}[(m.schedule||{}).type]||"—";
  document.getElementById("modeinfo").textContent=m.mode==="off"?"tous les modules sont arrêtés":"partage : "+sched;
  let b="";for(const k of ["adsb","ais","aprs","iss","beacon","elt121","433","openwebrx"]){const ok=((m.units||{})[k]||[]).length;
    b+=`<button class="${k===m.mode?"on":""}" ${(!ok||busy)?"disabled":""} onclick="setMode('${k}')">${L[k]}</button>`}
  b+=`<button class="stop" ${(m.mode==="off"||busy)?"disabled":""} onclick="setMode('off')">■ Arrêter</button>`;
  document.getElementById("btns").innerHTML=b;
  const a=d.activity||{};
  document.getElementById("act").innerHTML=
    tile("Avions",a.aircraft??"—",m.mode==="adsb"?"":"muted",m.mode==="adsb"?"":"en pause")+
    tile("Navires",a.ships??"—",m.mode==="ais"?"":"muted",m.mode==="ais"?"":"en pause")+
    tile("Stations APRS",a.aprs??"—","", "30 dernières min")+
    tile("Winlink",a.winlink_unread??"—",a.winlink_unread?"orange":"","non lu(s)")+
    (a.elt121?tile("121,5 MHz",a.elt121.active?"SIRÈNE":"veille",a.elt121.active?"red":"green",
      `niveau +${a.elt121.above_db??"—"} dB · ${a.elt121.events_today??0} détection(s) aujourd'hui`):"");
  const t=h.cpu_temp_c, pc=h.load&&h.cpu_count?Math.round(h.load[0]/h.cpu_count*100):null;
  const mem=h.mem_total?Math.round((h.mem_total-h.mem_available)/h.mem_total*100):null;
  const pw=th.under_voltage_now?["SOUS-TENSION","red"]:th.under_voltage_occurred?["À surveiller","orange"]:(h.throttled?["Correcte","green"]:["—","muted"]);
  document.getElementById("health").innerHTML=
    tile("Alimentation",pw[0],pw[1])+
    tile("Température",t!=null?t.toFixed(0)+" °C":"—",t>=80?"red":t>=70?"orange":"green")+
    tile("Charge CPU",pc!=null?pc+" %":"—",pc>=90?"red":pc>=70?"orange":"")+
    tile("Mémoire",mem!=null?mem+" %":"—","")+
    tile("Clés SDR",h.sdr_dongles??"—",h.sdr_dongles===0?"red":"",h.sdr_dongles===0?"aucune clé visible !":"");
  const v=d.vigilance||{};let vh="";
  if(v.status!=="ok"){vh=esc("Vigilance : "+(v.status||"—"))}
  else for(const p of v.periods||[]){const c=VC[p.max_color]||["#8b96a5","?"];
    vh+=`<div style="margin:4px 0"><b>${p.echeance==="J"?"Aujourd'hui":"Demain"}</b> <span class="pill" style="background:${c[0]}">${c[1]}</span> <span class="muted">${esc((p.phenomena||[]).map(x=>x.name+" ("+x.color_name+")").join(", ")||"rien de particulier")}</span></div>`}
  document.getElementById("vigi").innerHTML=vh||"—";
  const al=d.alerts||[];
  document.getElementById("alerts").innerHTML=al.length?al.map(x=>`<div class="alert"><div class="t">${new Date(x.time*1000).toLocaleString("fr-FR")}</div><b>${esc(x.title)}</b><div class="muted">${esc(x.message)}</div></div>`).join(""):"Aucune alerte récente.";
  document.getElementById("upd").textContent="Mis à jour à "+new Date().toLocaleTimeString("fr-FR")+" · actualisation toutes les 10 s";
}
async function load(){try{const r=await fetch("/api/dash");render(await r.json())}
  catch(e){document.getElementById("upd").innerHTML='<span class="err">Pi injoignable</span>'}}
load();setInterval(()=>{if(!busy)load()},10000);
</script></body></html>
"""


def dash_data() -> dict:
    """Résumé pour le tableau de bord du téléphone (une seule requête)."""
    mode = fetch_json("http://127.0.0.1:5006/api/sdr/mode") or {}
    health = fetch_json("http://127.0.0.1:5006/api/system/health") or {}
    m = mode.get("mode")
    activity = {}
    if m == "adsb":
        ac = read_aircraft() or {}
        activity["aircraft"] = sum(1 for a in ac.get("aircraft", []) if (a.get("seen") or 0) < 60)
    if m == "ais":
        ships = fetch_json("http://127.0.0.1:8100/api/ships.json") or {}
        activity["ships"] = len(ships.get("ships", [])) if isinstance(ships, dict) else None
    st = fetch_json("http://127.0.0.1:5004/api/aprs/stations")
    if isinstance(st, list):
        activity["aprs"] = sum(1 for x in st if time.time() - (x.get("last_heard") or 0) < 1800)
    inbox = fetch_json("http://127.0.0.1:8080/api/mailbox/in")
    if isinstance(inbox, list):
        activity["winlink_unread"] = sum(1 for x in inbox if x.get("Unread"))
    if m == "elt121":
        e = fetch_json("http://127.0.0.1:5011/api/elt121/status") or {}
        activity["elt121"] = {k: e.get(k) for k in ("active", "above_db", "freq_mhz", "score", "events_today")}
    with _lock:
        alerts = [a for a in _sent if time.time() - a["time"] < 48 * 3600][:10]
    return {"mode": mode, "health": health, "activity": activity, "vigilance": dict(_vigilance), "alerts": alerts}


def proxy_sdr_mode(mode: str):
    req = urllib.request.Request(f"http://127.0.0.1:5006/api/sdr/mode/{mode}", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        q = self._query()
        try:
            hours = float((q.get("hours") or ["24"])[0])
        except ValueError:
            hours = 24
        if path in ("", "/index.html"):
            body = DASH_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/dash":
            self._send(200, dash_data())
        elif path == "/api/stats/series":
            self._send(200, series(hours))
        elif path == "/api/stats/polar":
            kind = (q.get("kind") or ["adsb"])[0]
            self._send(200, polar(kind if kind in ("adsb", "ais") else "adsb", hours))
        elif path == "/api/monitor/config":
            self._send(200, public_config(load_config()))
        elif path == "/api/monitor/report":
            title, body = build_report()
            self._send(200, {"title": title, "body": body})
        elif path == "/api/vigilance":
            self._send(200, dict(_vigilance))
        elif path == "/api/monitor/notifications":
            with _lock:
                self._send(200, list(_sent))
        else:
            self._send(404, {"error": "introuvable"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            data = {}
        if path == "/api/monitor/config":
            self._send(200, public_config(update_config(data if isinstance(data, dict) else {})))
        elif path.startswith("/api/dash/sdr/"):
            mode = path.rsplit("/", 1)[-1]
            if mode not in ("adsb", "ais", "aprs", "openwebrx", "beacon", "elt121", "433", "iss", "off"):
                self._send(400, {"error": "module inconnu"})
                return
            try:
                self._send(200, proxy_sdr_mode(mode))
            except Exception as exc:
                self._send(502, {"error": str(exc)})
        elif path == "/api/monitor/report":
            title, body = build_report()
            self._send(200, notify(title, body, 2, force=True))
        elif path == "/api/monitor/test":
            res = notify("Test de notification",
                         "Si vous lisez ceci, les alertes de F4JCF-SDR-STATION arrivent bien. 73 !", 3, force=True)
            self._send(200, res)
        else:
            self._send(404, {"error": "introuvable"})

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    db().close()
    threading.Thread(target=collector_loop, daemon=True).start()
    threading.Thread(target=vigilance_loop, daemon=True).start()
    threading.Thread(target=report_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
