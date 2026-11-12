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
import re
import smtplib
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
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
               "elt121": True, "es_open": True, "aurora": True},
    # Vigilance Météo-France : clé gratuite à créer sur portail-api.meteofrance.fr
    # (API « Données Publiques Vigilance »).
    "vigilance": {"api_key": "", "dept": "06"},
    "daily_report": {"enabled": False, "hour": 20},
    # Dépôt sur GitHub (branche dédiée, dépôt public) : cartes WEFAX reçues par
    # radio et bilan hebdomadaire, lus par l'analyse météo du matin et le bilan
    # du dimanche. Jamais de POCSAG, de messages APRS ni de courrier Winlink.
    "github": {"enabled": False, "token": "", "repo": "F4JCF/STATION-SDR", "branch": "station-data"},
    # Station météo 433 MHz publiée sur APRS-IS (réseau CWOP / aprs.fi)
    "wx": {"enabled": False, "device": "", "ssid": "13", "comment": "STATION-SDR Mougins"},
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
    "es_open": "Ouverture sporadique-E (6 m, 4 m, 2 m)",
    "aurora": "Aurore possible (activité géomagnétique forte)",
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
    out["github"]["token_set"] = bool(cfg["github"].get("token"))
    out["github"].pop("token", None)
    out["event_labels"] = EVENT_LABELS
    return out


def update_config(data: dict) -> dict:
    cfg = load_config()
    for section in ("ntfy", "email", "events", "vigilance", "daily_report", "wx", "github"):
        if isinstance(data.get(section), dict):
            for k, v in data[section].items():
                if (section, k) in (("email", "password"), ("vigilance", "api_key"), ("github", "token")) and not v:
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
    _smtp_send(e, msg, timeout=20)


EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


class _SMTPv4(smtplib.SMTP):
    """SMTP en IPv4 uniquement (contourne une connexion IPv6 défaillante)."""
    def _get_socket(self, host, port, timeout):
        return _v4_socket(host, port, timeout, self.source_address)


class _SMTPv4_SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        raw = _v4_socket(host, port, timeout, self.source_address)
        return self.context.wrap_socket(raw, server_hostname=self._host)


def _v4_socket(host, port, timeout, source_address=None):
    err = None
    for af, st, proto, _cn, addr in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        sk = socket.socket(af, st, proto)
        sk.settimeout(timeout)
        try:
            if source_address:
                sk.bind(source_address)
            sk.connect(addr)
            return sk
        except OSError as exc:
            err = exc
            sk.close()
    raise err or OSError(f"aucune adresse IPv4 pour {host}")


def _smtp_try(e: dict, msg, timeout: int, v4: bool) -> None:
    host, port = e["smtp_host"], int(e.get("smtp_port") or 465)
    ssl_mode = bool(e.get("ssl", port == 465))
    stage = "connexion"
    try:
        if ssl_mode:
            cls = _SMTPv4_SSL if v4 else smtplib.SMTP_SSL
            sm = cls(host, port, timeout=timeout)
        else:
            cls = _SMTPv4 if v4 else smtplib.SMTP
            sm = cls(host, port, timeout=timeout)
        with sm:
            if not ssl_mode:
                stage = "STARTTLS"
                sm.ehlo()
                sm.starttls()
                sm.ehlo()
            stage = "identification"
            pw = (e.get("password") or "").strip()
            if "gmail" in host.lower() or "googlemail" in host.lower():
                # mot de passe d'application Google : 16 lettres, affichées en 4 groupes ;
                # le copier-coller ramène souvent des espaces (parfois insécables) refusés.
                pw = "".join(pw.split())
            sm.ehlo_or_helo_if_needed()
            sm.user, sm.password = e["user"].strip(), pw
            # Une seule méthode : sinon Gmail raccroche après le premier refus et
            # l'erreur réelle (535, mot de passe refusé) est masquée.
            mech = "PLAIN" if "plain" in sm.esmtp_features.get("auth", "").lower() else "LOGIN"
            sm.auth(mech, sm.auth_plain if mech == "PLAIN" else sm.auth_login)
            stage = "envoi"
            sm.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        detail = (exc.smtp_error or b"").decode(errors="ignore").split("\n")[0][:120]
        raise RuntimeError(f"identifiant ou mot de passe refusé par {host} ({exc.smtp_code} {detail}) — "
                           "pour Gmail, il faut un mot de passe d'application (16 lettres)") from exc
    except Exception as exc:
        try:
            ip = socket.getaddrinfo(host, port, socket.AF_INET if v4 else 0)[0][4][0]
        except OSError:
            ip = "?"
        raise RuntimeError(f"{stage} à {host}:{port} ({'SSL' if ssl_mode else 'STARTTLS'}, "
                           f"{'IPv4' if v4 else 'auto'} {ip}) : {type(exc).__name__} {exc}") from exc


def _smtp_send(e: dict, msg, timeout: int = 60) -> None:
    """Envoi SMTP. En cas d'échec de connexion, nouvel essai en IPv4 seul."""
    try:
        _smtp_try(e, msg, timeout, v4=False)
    except RuntimeError as first:
        if "identifiant ou mot de passe" in str(first):
            raise
        try:
            _smtp_try(e, msg, timeout, v4=True)
        except RuntimeError as second:
            raise RuntimeError(f"{first} | nouvel essai : {second}") from second
    log.info("e-mail envoyé via %s", e.get("smtp_host"))


def send_document(data: dict) -> dict:
    """Compte rendu de mission : e-mail avec la pièce jointe HTML (au
    destinataire configuré et/ou à des destinataires supplémentaires) et/ou
    ntfy (résumé + fichier, ouvrable sur le téléphone)."""
    cfg = load_config()
    subject = str(data.get("subject") or "Compte rendu")[:200]
    summary = str(data.get("summary") or "")[:4000]
    doc = str(data.get("html") or "")
    fname = re.sub(r"[^\w.-]+", "_", str(data.get("filename") or "compte_rendu.html"))[:120]
    if not fname.endswith(".html"):
        fname += ".html"
    if len(doc) > 12_000_000:
        raise ValueError("document trop volumineux")
    result = {"time": time.time(), "title": subject, "message": summary, "channels": {}}
    if data.get("email"):
        e = cfg["email"]
        extra = [a.strip() for a in re.split(r"[,;\s]+", str(data.get("extra_to") or "")) if a.strip()]
        bad = [a for a in extra if not EMAIL_RE.match(a)]
        to = ([a.strip() for a in re.split(r"[,;]+", e.get("to") or "") if a.strip()]
              if data.get("email_main", True) else [])
        to += [a for a in extra if a not in to and a not in bad]
        if bad:
            result["channels"]["adresses ignorées"] = ", ".join(bad)
        if not (e.get("smtp_host") and e.get("user") and e.get("password")):
            result["channels"]["email"] = "échec : e-mail non configuré (Notifications, page Santé du Pi)"
        elif not to:
            result["channels"]["email"] = "échec : aucun destinataire"
        else:
            try:
                msg = EmailMessage()
                msg["Subject"] = f"[F4JCF-SDR-STATION] {subject}"
                msg["From"] = e["user"]
                msg["To"] = ", ".join(to)
                msg.set_content(summary + "\n\nLe compte rendu complet (carte, relèvements, journal) est en pièce "
                                "jointe : l'ouvrir dans un navigateur, puis Imprimer → Enregistrer en PDF si besoin."
                                "\n\n-- \nF4JCF-SDR-STATION")
                msg.add_attachment(doc.encode("utf-8"), maintype="text", subtype="html", filename=fname)
                _smtp_send(e, msg)
                result["channels"]["email"] = "ok : " + ", ".join(to)
            except Exception as exc:
                result["channels"]["email"] = f"échec : {exc}"
    if data.get("ntfy"):
        n = cfg["ntfy"]
        if not n.get("topic"):
            result["channels"]["ntfy"] = "échec : aucun sujet ntfy configuré"
        else:
            try:
                q = {"title": subject, "message": summary[:3000], "tags": "clipboard"}
                body = b""
                if data.get("ntfy_file", True) and doc:
                    q["filename"] = fname
                    body = doc.encode("utf-8")
                url = f"{n.get('server', 'https://ntfy.sh').rstrip('/')}/{n['topic']}?{urllib.parse.urlencode(q)}"
                req = urllib.request.Request(url, data=body, method="PUT")
                with urllib.request.urlopen(req, timeout=60) as resp:
                    resp.read()
                result["channels"]["ntfy"] = "ok"
            except Exception as exc:
                result["channels"]["ntfy"] = f"échec : {exc}"
    log.info("Document « %s » : %s", subject, result["channels"])
    with _lock:
        _sent.insert(0, result)
        del _sent[50:]
    return result


def notify(title: str, message: str, priority: int = 4, event: str = None, force: bool = False) -> dict:
    cfg = load_config()
    if event == "pocsag":
        return {"channels": {}, "skipped": "POCSAG jamais relayé hors du réseau local"}
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


# ------------------------------------------- Station météo → APRS-WX --
#
# Les capteurs 433 MHz ne sont reçus que lorsque la clé SDR est sur le module
# « 433 » (rotation) : on lit le fichier des capteurs, et on ne publie que des
# valeurs de moins de 30 minutes, au plus toutes les 10 minutes.

RTL433_STORE = os.environ.get("RTL433_STORE", "/var/lib/rtl433/devices.json")
APRS_IS = ("rotate.aprs2.net", 14580)
_wx = {"last_sent": None, "last_packet": None, "last_error": None, "rain": []}


def aprs_passcode(call: str) -> int:
    base = call.upper().split("-")[0]
    h = 0x73E2
    for i in range(0, len(base), 2):
        h ^= ord(base[i]) << 8
        if i + 1 < len(base):
            h ^= ord(base[i + 1])
    return h & 0x7FFF


def wx_devices() -> dict:
    try:
        with open(RTL433_STORE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    try:
        live = fetch_json("http://127.0.0.1:5010/api/433/devices", 3)
        for d in live if isinstance(live, list) else (live or {}).values():
            if isinstance(d, dict) and d.get("key"):
                data[d["key"]] = d
    except Exception:
        pass
    return data


def wx_values(dev: dict) -> dict:
    """Valeurs normalisées (°C, %, hPa, km/h, degrés, mm cumulés)."""
    v = dev.get("values") or {}
    out = {}

    def num(*keys):
        for k in keys:
            if isinstance(v.get(k), (int, float)):
                return float(v[k])
        return None
    t = num("temperature_C", "temperature_1_C")
    if t is None and num("temperature_F") is not None:
        t = (num("temperature_F") - 32) * 5 / 9
    out["temp_c"] = t
    out["humidity"] = num("humidity", "humidity_1")
    p = num("pressure_hPa")
    if p is None and num("pressure_kPa") is not None:
        p = num("pressure_kPa") * 10
    out["pressure_hpa"] = p
    w = num("wind_avg_km_h")
    if w is None and num("wind_avg_m_s", "wind_speed_m_s") is not None:
        w = num("wind_avg_m_s", "wind_speed_m_s") * 3.6
    if w is None and num("wind_avg_mi_h") is not None:
        w = num("wind_avg_mi_h") * 1.609344
    out["wind_kmh"] = w
    g = num("wind_max_km_h")
    if g is None and num("wind_max_m_s", "gust_speed_m_s") is not None:
        g = num("wind_max_m_s", "gust_speed_m_s") * 3.6
    if g is None and num("wind_max_mi_h") is not None:
        g = num("wind_max_mi_h") * 1.609344
    out["gust_kmh"] = g
    out["wind_dir"] = num("wind_dir_deg")
    r = num("rain_mm")
    if r is None and num("rain_in") is not None:
        r = num("rain_in") * 25.4
    out["rain_mm"] = r
    return {k: v for k, v in out.items() if v is not None}


def wx_candidates() -> list:
    out = []
    for key, d in wx_devices().items():
        if "tpms" in str(d.get("model", "")).lower():
            continue                      # capteurs de pneus : pas une station météo
        vals = wx_values(d)
        if "temp_c" in vals or "humidity" in vals or "wind_kmh" in vals:
            out.append({"key": key, "model": d.get("model"), "id": d.get("id"), "channel": d.get("channel"),
                        "last_seen": d.get("last_seen"), "values": vals})
    out.sort(key=lambda d: -(d.get("last_seen") or 0))
    return out


def _deg_min(value: float, lat: bool) -> str:
    hemi = ("N" if value >= 0 else "S") if lat else ("E" if value >= 0 else "W")
    a = abs(value)
    deg = int(a)
    mins = (a - deg) * 60
    return f"{deg:02d}{mins:05.2f}{hemi}" if lat else f"{deg:03d}{mins:05.2f}{hemi}"


def wx_packet(cfg: dict, vals: dict, when: float) -> str:
    call = f"{cfg['mycall'].split('-')[0]}-{cfg['wx'].get('ssid') or '13'}"
    stamp = time.strftime("%d%H%M", time.gmtime(when))
    pos = f"{_deg_min(cfg['station_lat'], True)}/{_deg_min(cfg['station_lon'], False)}"
    wd = f"{int(round(vals['wind_dir'])) % 360:03d}" if "wind_dir" in vals else "..."
    ws = f"{int(round(vals['wind_kmh'] / 1.609344)):03d}" if "wind_kmh" in vals else "..."
    body = f"_{wd}/{ws}"
    body += f"g{int(round(vals['gust_kmh'] / 1.609344)):03d}" if "gust_kmh" in vals else "g..."
    if "temp_c" in vals:
        tf = int(round(vals["temp_c"] * 9 / 5 + 32))
        body += f"t{tf:03d}" if tf >= 0 else f"t-{abs(tf):02d}"
    else:
        body += "t..."
    rain_h = wx_rain_hour(vals.get("rain_mm"), when)
    if rain_h is not None:
        body += f"r{min(999, int(round(rain_h / 0.254))):03d}"
    if "humidity" in vals:
        h = int(round(vals["humidity"]))
        body += f"h{0 if h >= 100 else max(1, h):02d}"
    if "pressure_hpa" in vals:
        body += f"b{int(round(vals['pressure_hpa'] * 10)):05d}"
    comment = re.sub(r"[^\x20-\x7e]", "", str(cfg["wx"].get("comment") or ""))[:40]
    return f"{call}>APZSDR,TCPIP*:@{stamp}z{pos}{body}{comment}"


def wx_rain_hour(total, when):
    """Pluie de la dernière heure (mm) à partir du cumul, si on a un relevé d'il y a ~1 h."""
    if total is None:
        return None
    hist = _wx["rain"]
    if not hist or hist[-1][1] != total or when - hist[-1][0] > 600:
        hist.append((when, total))
    del hist[:-200]
    old = [h for h in hist if 3000 <= when - h[0] <= 4500]
    if not old:
        return None
    diff = total - old[0][1]
    return diff if diff >= 0 else None      # remise à zéro du cumul : on s'abstient


def aprs_is_send(call: str, lines: list) -> None:
    import socket
    with socket.create_connection(APRS_IS, timeout=20) as sk:
        sk.settimeout(20)
        sk.recv(512)
        sk.sendall(f"user {call} pass {aprs_passcode(call)} vers STATION-SDR 1.32\r\n".encode())
        resp = b""
        end = time.time() + 8
        while b"logresp" not in resp and time.time() < end:
            try:
                chunk = sk.recv(512)
            except socket.timeout:
                break
            if not chunk:
                break
            resp += chunk
        if b" unverified" in resp:
            raise RuntimeError("APRS-IS : indicatif non vérifié")
        for line in lines:
            sk.sendall((line + "\r\n").encode("ascii", "replace"))
        time.sleep(1)


def wx_status() -> dict:
    cfg = load_config()
    w = cfg["wx"]
    cands = wx_candidates()
    sel = next((c for c in cands if c["key"] == w.get("device")), None)
    return {"config": w, "call": f"{cfg['mycall'].split('-')[0]}-{w.get('ssid') or '13'}", "devices": cands,
            "selected": sel, "last_sent": _wx["last_sent"], "last_packet": _wx["last_packet"],
            "last_error": _wx["last_error"]}


def wx_loop() -> None:
    time.sleep(30)
    while True:
        try:
            cfg = load_config()
            w = cfg["wx"]
            if w.get("enabled") and w.get("device"):
                dev = wx_devices().get(w["device"])
                fresh = dev and time.time() - (dev.get("last_seen") or 0) < 1800
                due = not _wx["last_sent"] or time.time() - _wx["last_sent"] >= 600
                if fresh and due:
                    vals = wx_values(dev)
                    if vals:
                        pkt = wx_packet(cfg, vals, dev["last_seen"])
                        aprs_is_send(pkt.split(">", 1)[0], [pkt])
                        _wx.update(last_sent=time.time(), last_packet=pkt, last_error=None)
                        log.info("APRS-WX : %s", pkt)
                elif dev and not fresh:
                    _wx["last_error"] = "valeurs de plus de 30 min (clé hors 433 MHz) : envoi suspendu"
        except Exception as exc:
            _wx["last_error"] = str(exc)
            log.warning("APRS-WX : %s", exc)
        time.sleep(60)


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


# Colonnes ajoutées au fil des versions (modules 433, POCSAG, balises).
EXTRA_COLS = [("sensors433", "INTEGER"), ("pocsag_msgs", "INTEGER"), ("beacon_frames", "INTEGER"),
              ("elt_above", "REAL"), ("elt_active", "INTEGER")]
SERIES_COLS = ["t", "aircraft", "aircraft_pos", "adsb_max_km", "ships", "ais_max_km", "aprs_stations",
               "temp", "undervolt", "mode", "dongles"] + [c for c, _t in EXTRA_COLS]


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS samples (
        t INTEGER PRIMARY KEY, aircraft INTEGER, aircraft_pos INTEGER, adsb_max_km REAL,
        ships INTEGER, ais_max_km REAL, aprs_stations INTEGER, temp REAL, undervolt INTEGER,
        mode TEXT, dongles INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS polar (
        kind TEXT, hour INTEGER, sector INTEGER, max_km REAL, PRIMARY KEY (kind, hour, sector))""")
    have = {r[1] for r in con.execute("PRAGMA table_info(samples)")}
    for col, typ in EXTRA_COLS:
        if col not in have:
            con.execute(f"ALTER TABLE samples ADD COLUMN {col} {typ}")
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

    # --- Autres modules (seulement quand ils ont la clé)
    sensors433 = pocsag_msgs = beacon_frames = elt_above = elt_active = None
    if mode == "433":
        devs = fetch_json("http://127.0.0.1:5010/api/433/devices")
        if isinstance(devs, list):
            sensors433 = sum(1 for d in devs if now - (d.get("last_seen") or 0) < 1800)
    elif mode == "pocsag":
        msgs = fetch_json("http://127.0.0.1:5012/api/pocsag/messages?limit=1000")
        if isinstance(msgs, list):
            pocsag_msgs = sum(1 for m in msgs if now - (m.get("time") or 0) < 1800 and not m.get("tech"))
    elif mode == "beacon":
        frames = fetch_json("http://127.0.0.1:5009/api/beacon/frames?limit=500")
        if isinstance(frames, list):
            beacon_frames = sum(1 for f in frames if not f.get("manual") and now - (f.get("time") or 0) < 3600)
    elif mode == "elt121":
        est = fetch_json("http://127.0.0.1:5011/api/elt121/status") or {}
        if est.get("running"):
            elt_above = est.get("above_db")
            elt_active = 1 if est.get("active") else 0

    th = health.get("throttled") or {}
    con = db()
    try:
        adsb_max = record_polar(con, "adsb", hour, ac_pos, lat0, lon0) if mode == "adsb" else None
        ais_max = record_polar(con, "ais", hour, ship_pos, lat0, lon0) if mode == "ais" else None
        con.execute(
            f"INSERT OR REPLACE INTO samples ({','.join(SERIES_COLS)}) VALUES ({','.join('?' * len(SERIES_COLS))})",
            (now, len(aircraft) if mode == "adsb" else None, len(ac_pos) if mode == "adsb" else None, adsb_max,
             len(ships) if mode == "ais" else None, ais_max, aprs_recent if mode in ("aprs", "iss") else None,
             health.get("cpu_temp_c"), 1 if th.get("under_voltage_now") else 0, mode, health.get("sdr_dongles"),
             sensors433, pocsag_msgs, beacon_frames, elt_above, elt_active),
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
            exo = abs(float(e.get("freq_mhz") or 0) - 121.375) < 0.001
            notify(("Sirène d'EXERCICE" if exo else "Sirène de balise") + f" sur {e.get('freq_mhz')} MHz",
                   f"Détectée à {time.strftime('%H:%M:%S', time.localtime(e['start']))} sur "
                   f"{e.get('freq_mhz')} MHz, {e.get('max_above_db')} dB au-dessus du bruit, "
                   f"{e.get('sweeps_per_s')} balayages/s.\nEnregistrement disponible dans l'appli "
                   "(Balises de détresse → 121,5 MHz).", 4, "elt121")

    # POCSAG : volontairement JAMAIS envoyé vers ntfy / e-mail. Les messages captés
    # (radiomessagerie professionnelle, SDIS…) relèvent du secret des correspondances :
    # ils restent sur le réseau local, consultables seulement dans l'appli (page POCSAG).

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
            # Sans clé Météo-France : vigilances officielles rediffusées par MeteoAlarm
            try:
                periods = meteoalarm_periods(dept)
                _vigilance.update(status="ok", source="MeteoAlarm", fetched_at=time.time(), periods=periods,
                                  dept=dept)
                for per in periods:
                    if per["max_color"] >= 3:
                        phen = ", ".join(f"{p['name']} ({p['color_name']})" for p in per["phenomena"]
                                         if p["color"] >= 3)
                        key_ev = f"vigi-ma:{per.get('begin')}:{per['max_color']}:{phen}"
                        if event_once(key_ev, 12 * 3600):
                            notify(f"Vigilance {per['max_color_name'].upper()} — dép. {dept}",
                                   f"Météo-France place le département {dept} en vigilance "
                                   f"{per['max_color_name']} : {phen} (du {per.get('begin_txt')} au "
                                   f"{per.get('end_txt')}). Détails : vigilance.meteofrance.fr",
                                   5 if per["max_color"] == 4 else 4, "vigilance")
            except Exception as exc:
                _vigilance.update(status=f"MeteoAlarm injoignable : {exc}", periods=[])
            for _ in range(1800 // 5):
                time.sleep(5)
                if load_config()["vigilance"].get("api_key"):
                    break
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


METEOALARM_URL = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-france"
DEPT_NAMES = {"06": ("Alpes-Maritimes", "FRL03"), "83": ("Var", "FRL05"), "04": ("Alpes-de-Haute-Provence", "FRL01"),
              "13": ("Bouches-du-Rhône", "FRL04"), "05": ("Hautes-Alpes", "FRL02"), "84": ("Vaucluse", "FRL06")}


def meteoalarm_periods(dept: str) -> list:
    """Vigilances Météo-France en cours ou à venir pour un département (MeteoAlarm, sans clé),
    au même format que parse_vigilance."""
    name, nuts = DEPT_NAMES.get(dept, ("Alpes-Maritimes", "FRL03"))
    data = json.loads(urllib.request.urlopen(urllib.request.Request(
        METEOALARM_URL, headers={"User-Agent": "STATION-SDR F4JCF"}), timeout=30).read())
    now = time.time()
    out = {}
    for w in data.get("warnings", []):
        for info in w.get("alert", {}).get("info", []):
            if not str(info.get("language", "")).startswith("fr"):
                continue
            if not any(a.get("areaDesc") == name or any(g.get("value") == nuts for g in a.get("geocode", []))
                       for a in info.get("area", [])):
                continue
            lvl = next((p.get("value") for p in info.get("parameter", []) if p.get("valueName") == "awareness_level"), "")
            try:
                color = int(str(lvl).split(";")[0])
            except ValueError:
                continue
            try:
                from datetime import datetime
                end = datetime.fromisoformat(info["expires"]).timestamp()
                begin = datetime.fromisoformat(info.get("onset") or info.get("effective")).timestamp()
            except Exception:
                end, begin = now + 3600, now
            if color < 2 or end < now:
                continue
            ev = re.sub(r"^Vigilance (verte|jaune|orange|rouge) ", "", info.get("event") or "?")
            per = out.setdefault(int(begin), {"echeance": "J" if begin < now + 86400 else "J1", "begin": int(begin),
                                              "begin_txt": time.strftime("%d/%m %H:%M", time.localtime(begin)),
                                              "end_txt": time.strftime("%d/%m %H:%M", time.localtime(end)),
                                              "phenomena": [], "max_color": 1})
            if not any(p["name"] == ev for p in per["phenomena"]):
                per["phenomena"].append({"name": ev, "color": color, "color_name": VIGI_COLORS.get(color, "?")})
            per["max_color"] = max(per["max_color"], color)
    for per in out.values():
        per["max_color_name"] = VIGI_COLORS.get(per["max_color"], "?")
    return sorted(out.values(), key=lambda p: p["begin"])


# ------------------------------------------- Propagation VHF / aurores --
#
# HamQSL (N0NBH) publie un état « E-Skip » pour l'Europe sur 6 m, 4 m et 2 m et
# un état « aurore » ; la NOAA publie le Kp planétaire. On ne prévient qu'au
# passage de fermé à ouvert, au plus une fois toutes les 6 h par bande.

HAMQSL_URL = "https://www.hamqsl.com/solarxml.php"
KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
ES_BANDS = {"europe_6m": "6 m (50 MHz)", "europe_4m": "4 m (70 MHz)", "europe": "2 m (144 MHz)"}
_space = {"status": "pas encore lu", "vhf": {}, "kp": None, "fetched_at": None}


def _fetch_space():
    import xml.etree.ElementTree as ET
    hdr = {"User-Agent": "STATION-SDR F4JCF"}
    root = ET.fromstring(urllib.request.urlopen(urllib.request.Request(HAMQSL_URL, headers=hdr), timeout=30).read())
    sd = root.find("solardata")
    vhf = {}
    for b in sd.find("calculatedvhfconditions"):
        vhf[(b.get("name"), b.get("location"))] = (b.text or "").strip()
    kp = None
    try:
        rows = json.loads(urllib.request.urlopen(urllib.request.Request(KP_URL, headers=hdr), timeout=30).read())
        last = rows[-1]
        kp = float(last["Kp"] if isinstance(last, dict) else last[1])
    except Exception:
        pass
    return vhf, kp, {c.tag: (c.text or "").strip() for c in sd if c.tag in ("solarflux", "kindex", "aindex", "geomagfield")}


def space_loop() -> None:
    prev = None
    last_alert = {}
    while True:
        try:
            vhf, kp, solar = _fetch_space()
            _space.update(status="ok", vhf={f"{k[0]}/{k[1]}": v for k, v in vhf.items()}, kp=kp, solar=solar,
                          fetched_at=time.time())
            now = time.time()
            state = {}
            for loc, label in ES_BANDS.items():
                st = vhf.get(("E-Skip", loc), "")
                state[loc] = bool(st) and "closed" not in st.lower()
            aur_txt = vhf.get(("vhf-aurora", "northern_hemi"), "")
            state["aurora"] = (bool(aur_txt) and "closed" not in aur_txt.lower()) or (kp is not None and kp >= 6)
            if prev is not None:
                opened = [loc for loc in ES_BANDS if state[loc] and not prev.get(loc)
                          and now - last_alert.get(loc, 0) > 6 * 3600]
                if opened:
                    for loc in opened:
                        last_alert[loc] = now
                    bands = ", ".join(ES_BANDS[l] for l in opened)
                    detail = "; ".join(f"{ES_BANDS[l]} : {vhf.get(('E-Skip', l))}" for l in opened)
                    notify(f"Sporadique-E : ouverture {bands}",
                           f"HamQSL signale une ouverture sporadique-E en Europe ({detail}). "
                           f"Bon moment pour écouter et appeler en {bands.split(' (')[0]}. 73 !", 4, "es_open")
                if state["aurora"] and not prev.get("aurora") and now - last_alert.get("aurora", 0) > 12 * 3600:
                    last_alert["aurora"] = now
                    notify("Aurore possible — activité géomagnétique forte",
                           f"Kp = {kp if kp is not None else '?'} ; HamQSL VHF aurore : {aur_txt or '?'}. "
                           "Propagation aurorale possible en VHF (au nord), HF perturbée surtout aux "
                           "hautes latitudes.", 4, "aurora")
            prev = state
        except Exception as exc:
            _space.update(status=f"erreur : {exc}")
        time.sleep(1800)


# ------------------------------------------------- Dépôt sur GitHub --
#
# Branche dédiée (sans historique de code) du dépôt public : cartes WEFAX reçues
# par radio (Pi et PC) et bilan hebdomadaire. API REST « contents » : pas de git.

GH_API = "https://api.github.com"
GH_STATE = os.path.join(os.path.dirname(DB_PATH), "github_state.json")
WEFAX_PC_DIR = os.path.join(os.path.dirname(DB_PATH), "wefax_pc")
METEOR_API = "http://127.0.0.1:5013"
_gh = {"status": "non configuré", "last": None}


def _gh_req(cfg, method, url, body=None, timeout=60):
    g = cfg["github"]
    req = urllib.request.Request(GH_API + url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {g['token'].strip()}",
                                          "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28",
                                          "User-Agent": "STATION-SDR F4JCF"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        detail = exc.read()[:200].decode(errors="ignore")
        raise RuntimeError(f"GitHub {exc.code} : {detail}") from exc


def _gh_ensure_branch(cfg):
    g = cfg["github"]
    repo, br = g["repo"], g["branch"]
    if _gh_req(cfg, "GET", f"/repos/{repo}/git/ref/heads/{br}"):
        return
    readme = ("# Données de la station F4JCF-SDR-STATION\n\nCartes WEFAX reçues par radio et bilans "
              "hebdomadaires, déposés automatiquement par le Pi (station_monitor). Branche sans code.\n")
    blob = _gh_req(cfg, "POST", f"/repos/{repo}/git/blobs", {"content": readme, "encoding": "utf-8"})
    tree = _gh_req(cfg, "POST", f"/repos/{repo}/git/trees",
                   {"tree": [{"path": "README.md", "mode": "100644", "type": "blob", "sha": blob["sha"]}]})
    commit = _gh_req(cfg, "POST", f"/repos/{repo}/git/commits",
                     {"message": "Création de la branche des données de la station", "tree": tree["sha"], "parents": []})
    _gh_req(cfg, "POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{br}", "sha": commit["sha"]})


def gh_put(cfg, path, data: bytes, message):
    import base64
    g = cfg["github"]
    url = f"/repos/{g['repo']}/contents/{urllib.parse.quote(path)}"
    cur = _gh_req(cfg, "GET", f"{url}?ref={g['branch']}")
    body = {"message": message, "branch": g["branch"], "content": base64.b64encode(data).decode()}
    if isinstance(cur, dict) and cur.get("sha"):
        body["sha"] = cur["sha"]
    _gh_req(cfg, "PUT", url, body, timeout=120)


def gh_prune(cfg, folder, keep):
    g = cfg["github"]
    items = _gh_req(cfg, "GET", f"/repos/{g['repo']}/contents/{folder}?ref={g['branch']}") or []
    files = sorted((i for i in items if i.get("type") == "file" and i["name"] != "index.json"),
                   key=lambda i: i["name"], reverse=True)
    for i in files[keep:]:
        _gh_req(cfg, "DELETE", f"/repos/{g['repo']}/contents/{urllib.parse.quote(i['path'])}",
                {"message": f"Nettoyage : {i['name']}", "sha": i["sha"], "branch": g["branch"]})


def _gh_state():
    try:
        with open(GH_STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _gh_save_state(st):
    tmp = GH_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, GH_STATE)


def build_week(days: int = 7) -> dict:
    """Bilan des 7 derniers jours (sans POCSAG, messages APRS ni courrier)."""
    end = int(time.time())
    since = end - days * 86400
    con = db()
    try:
        rows = con.execute("SELECT * FROM samples WHERE t >= ? ORDER BY t", (since,)).fetchall()
        polar_rows = con.execute("SELECT kind, sector, MAX(max_km) FROM polar GROUP BY kind, sector").fetchall()
    finally:
        con.close()
    per_day = {}
    for r in rows:
        d = time.strftime("%Y-%m-%d", time.localtime(r[0]))
        x = per_day.setdefault(d, {"minutes": 0, "avions_max": 0, "adsb_portee_km": 0, "navires_max": 0,
                                   "ais_portee_km": 0, "aprs_stations_max": 0, "temp_min": None, "temp_max": None,
                                   "sous_tension_min": 0, "cle_absente_min": 0, "modes": {}})
        x["minutes"] += 1
        x["avions_max"] = max(x["avions_max"], r[1] or 0)
        x["adsb_portee_km"] = round(max(x["adsb_portee_km"], r[3] or 0))
        x["navires_max"] = max(x["navires_max"], r[4] or 0)
        x["ais_portee_km"] = round(max(x["ais_portee_km"], r[5] or 0))
        x["aprs_stations_max"] = max(x["aprs_stations_max"], r[6] or 0)
        if r[7] is not None:
            x["temp_min"] = r[7] if x["temp_min"] is None else min(x["temp_min"], r[7])
            x["temp_max"] = r[7] if x["temp_max"] is None else max(x["temp_max"], r[7])
        x["sous_tension_min"] += 1 if r[8] else 0
        x["cle_absente_min"] += 1 if r[10] == 0 else 0
        m = MODE_NAMES.get(r[9] or "?", r[9] or "?")
        x["modes"][m] = x["modes"].get(m, 0) + 1
    week = {"genere": time.strftime("%Y-%m-%d %H:%M %Z"), "du": time.strftime("%Y-%m-%d", time.localtime(since)),
            "au": time.strftime("%Y-%m-%d", time.localtime(end)), "station": load_config().get("mycall"),
            "jours": per_day}
    def best(key):
        vals = [(v[key], d) for d, v in per_day.items() if v[key]]
        return {"valeur": max(vals)[0], "jour": max(vals)[1]} if vals else None
    week["records"] = {"adsb_portee_km": best("adsb_portee_km"), "ais_portee_km": best("ais_portee_km"),
                       "avions_simultanes": best("avions_max"), "navires_simultanes": best("navires_max"),
                       "aprs_stations": best("aprs_stations_max")}
    sectors = {}
    for kind, sector, mk in polar_rows:
        sectors.setdefault(kind, {})[f"{int(sector) * 10:03d}-{int(sector) * 10 + 10:03d}°"] = round(mk or 0)
    week["portee_par_secteur_30j_km"] = sectors     # secteurs de 10° (relèvement depuis la station, 0° = nord)
    frames = fetch_json("http://127.0.0.1:5009/api/beacon/frames?limit=1000")
    if isinstance(frames, list):
        rec = [f for f in frames if not f.get("manual") and (f.get("time") or 0) >= since]
        week["balises_406"] = {"reelles": len({f.get("hex_id") for f in rec if not f.get("test")}),
                               "test": len({f.get("hex_id") for f in rec if f.get("test")})}
    elt = fetch_json("http://127.0.0.1:5011/api/elt121/events")
    if isinstance(elt, list):
        week["sirene_121_5_detections"] = sum(1 for e in elt if (e.get("start") or 0) >= since)
    sensors = fetch_json("http://127.0.0.1:5010/api/433/devices")
    if isinstance(sensors, list):
        week["capteurs_433_actifs"] = sum(1 for d in sensors if (d.get("last_seen") or 0) >= since)
    imgs = fetch_json(METEOR_API + "/api/meteor/images")
    if isinstance(imgs, list):
        recent = [i for i in imgs if (i.get("aos") or 0) >= since]
        kinds = {}
        for i in recent:
            kinds[i.get("kind") or "?"] = kinds.get(i.get("kind") or "?", 0) + 1
        week["images_recues_pi"] = kinds
    try:
        week["cartes_wefax_pc"] = sum(1 for f in os.listdir(WEFAX_PC_DIR)
                                      if os.path.getmtime(os.path.join(WEFAX_PC_DIR, f)) >= since)
    except OSError:
        week["cartes_wefax_pc"] = 0
    with _lock:
        alerts = [a for a in _sent if a["time"] >= since and a["title"] != "Test de notification"]
    week["alertes"] = [{"quand": time.strftime("%Y-%m-%d %H:%M", time.localtime(a["time"])), "titre": a["title"]}
                       for a in alerts]
    return week


def github_loop() -> None:
    """Toutes les 15 min : cartes WEFAX nouvelles ; le dimanche dès 5 h : bilan de la semaine."""
    while True:
        cfg = load_config()
        g = cfg.get("github") or {}
        if not (g.get("enabled") and g.get("token")):
            _gh.update(status="désactivé" if not g.get("enabled") else "jeton manquant")
            time.sleep(60)
            continue
        try:
            _gh_ensure_branch(cfg)
            st = _gh_state()
            done = set(st.get("wefax", []))
            n = 0
            imgs = fetch_json(METEOR_API + "/api/meteor/images") or []
            for it in imgs if isinstance(imgs, list) else []:
                if it.get("kind") != "wefax" or it["name"] in done:
                    continue
                with urllib.request.urlopen(f"{METEOR_API}/api/meteor/image/{urllib.parse.quote(it['name'])}",
                                            timeout=60) as r:
                    png = r.read()
                stamp = time.strftime("%Y%m%d-%H%M", time.gmtime(it.get("aos") or time.time()))
                gh_put(cfg, f"wefax/RADIO_{stamp}Z_pi.png", png, f"Carte WEFAX reçue par le Pi ({it.get('sat')})")
                done.add(it["name"]); n += 1
            if os.path.isdir(WEFAX_PC_DIR):
                for f in sorted(os.listdir(WEFAX_PC_DIR)):
                    if f in done or not f.endswith(".png"):
                        continue
                    with open(os.path.join(WEFAX_PC_DIR, f), "rb") as fh:
                        gh_put(cfg, f"wefax/{f}", fh.read(), "Carte WEFAX reçue par le PC")
                    done.add(f); n += 1
            if n:
                gh_prune(cfg, "wefax", 40)
            st["wefax"] = sorted(done)[-300:]
            lt = time.localtime()
            week_key = time.strftime("%G-W%V", lt)
            if lt.tm_wday == 6 and lt.tm_hour >= 5 and st.get("week") != week_key:
                data = json.dumps(build_week(), ensure_ascii=False, indent=1).encode()
                gh_put(cfg, f"semaine/{time.strftime('%Y-%m-%d', lt)}.json", data, "Bilan de la semaine")
                gh_put(cfg, "semaine/derniere.json", data, "Bilan de la semaine (dernier)")
                gh_prune(cfg, "semaine", 13)
                st["week"] = week_key
            _gh_save_state(st)
            _gh.update(status="ok", last=time.time(), sent=n)
        except Exception as exc:
            _gh.update(status=f"erreur : {exc}")
            log.warning("Dépôt GitHub : %s", exc)
        time.sleep(900)


def save_pc_wefax(data: dict) -> dict:
    import base64
    name = re.sub(r"[^0-9A-Za-z_.,-]", "_", str(data.get("name") or ""))[:80]
    png = base64.b64decode(data.get("png_b64") or "")
    if not png.startswith(b"\x89PNG") or len(png) > 20_000_000:
        raise ValueError("image PNG attendue")
    os.makedirs(WEFAX_PC_DIR, exist_ok=True)
    fn = f"RADIO_{name or time.strftime('%Y%m%d-%H%M')}_pc.png"
    with open(os.path.join(WEFAX_PC_DIR, fn), "wb") as f:
        f.write(png)
    files = sorted(os.listdir(WEFAX_PC_DIR))
    for old in files[:-60]:
        try:
            os.remove(os.path.join(WEFAX_PC_DIR, old))
        except OSError:
            pass
    return {"ok": True, "file": fn}


# ------------------------------------------------------ Rapport quotidien --

REPORT_STATE = os.path.join(os.path.dirname(DB_PATH), "report_last")
MODE_NAMES = {"adsb": "ADS-B", "ais": "AIS", "aprs": "APRS", "iss": "APRS ISS", "beacon": "Balise 406",
              "433": "433 MHz", "elt121": "Balise 121,5", "pocsag": "POCSAG", "radiosonde": "Radiosondes", "meteor": "Images satellites", "ecoute": "Écoute", "openwebrx": "OpenWebRX", "off": "arrêtée"}


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
        rows = con.execute(f"SELECT {','.join(SERIES_COLS)} FROM samples WHERE t >= ? ORDER BY t",
                           (since,)).fetchall()
    finally:
        con.close()
    cols = list(SERIES_COLS)
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
            merged.append(tuple([t] + [rs[-1][i] if cols[i] == "mode" else agg(i, min if cols[i] == "dongles" else max)
                                       for i in range(1, len(cols))]))
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
<div class="card"><div class="lbl">Écoute à distance</div>
<div class="row" style="margin-top:6px;align-items:center">
<select id="lpre" onchange="pre()" style="font:inherit;padding:9px;border-radius:9px;background:var(--alt);color:var(--text);border:1px solid var(--border)">
<option value="">Fréquence…</option><option value="145.500|fm">145,500 FM · appel 2 m</option>
<option value="433.500|fm">433,500 FM · appel 70 cm</option><option value="144.800|fm">144,800 FM · APRS</option>
<option value="145.825|fm">145,825 FM · APRS ISS</option><option value="145.800|fm">145,800 FM · ISS (phonie, SSTV)</option>
<option value="121.375|am">121,375 AM · exercice ADRASEC</option><option value="121.500|am">121,500 AM · détresse</option></select>
<input id="lf" inputmode="decimal" placeholder="MHz" style="width:110px;font:inherit;padding:9px;border-radius:9px;background:var(--alt);color:var(--text);border:1px solid var(--border)">
<select id="lm" style="font:inherit;padding:9px;border-radius:9px;background:var(--alt);color:var(--text);border:1px solid var(--border)"><option value="fm">FM</option><option value="am">AM</option></select>
<select id="lsq" style="font:inherit;padding:9px;border-radius:9px;background:var(--alt);color:var(--text);border:1px solid var(--border)"><option value="0">Silencieux : ouvert</option><option value="60">Silencieux : léger</option><option value="150">Silencieux : moyen</option><option value="300">Silencieux : fort</option></select>
</div>
<div class="btns"><button class="on" onclick="listen()">▶ Écouter</button><button id="lelt" style="display:none" onclick="listenElt()">▶ Écouter la veille 121,5</button>
<button class="stop" onclick="stopListen()">■ Arrêter l'écoute</button></div>
<audio id="aud" controls preload="none" style="width:100%;margin-top:10px;display:none"></audio>
<div class="muted" id="linfo" style="font-size:12px;margin-top:6px">Le son passe par votre réseau ou le VPN. La clé SDR revient seule au module précédent 5 min après la fin de l'écoute.</div></div>
<div class="card"><div class="lbl">Activité</div><div class="row" id="act"></div></div>
<div class="card"><div class="lbl">Santé du Pi</div><div class="row" id="health"></div></div>
<div class="card"><div class="lbl">Vigilance Météo-France</div><div id="vigi" class="muted">—</div></div>
<div class="card"><div class="lbl">Dernières alertes</div><div id="alerts" class="muted">—</div></div>
<script>
const L={adsb:"ADS-B",ais:"AIS",aprs:"APRS",openwebrx:"OpenWebRX",beacon:"Balise 406",elt121:"121,5 MHz",pocsag:"POCSAG",radiosonde:"Radiosondes",meteor:"Images sat.","433":"433 MHz",iss:"APRS ISS",ecoute:"Écoute",off:"Arrêtée"};
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
  let b="";for(const k of ["adsb","ais","aprs","iss","beacon","elt121","pocsag","radiosonde","433","openwebrx"]){const ok=((m.units||{})[k]||[]).length;
    b+=`<button class="${k===m.mode?"on":""}" ${(!ok||busy)?"disabled":""} onclick="setMode('${k}')">${L[k]}</button>`}
  b+=`<button class="stop" ${(m.mode==="off"||busy)?"disabled":""} onclick="setMode('off')">■ Arrêter</button>`;
  document.getElementById("btns").innerHTML=b;
  document.getElementById("lelt").style.display=m.mode==="elt121"?"inline-block":"none";
  const a=d.activity||{};
  document.getElementById("act").innerHTML=
    tile("Avions",a.aircraft??"—",m.mode==="adsb"?"":"muted",m.mode==="adsb"?"":"en pause")+
    tile("Navires",a.ships??"—",m.mode==="ais"?"":"muted",m.mode==="ais"?"":"en pause")+
    tile("Stations APRS",a.aprs??"—","", "30 dernières min")+
    tile("Winlink",a.winlink_unread??"—",a.winlink_unread?"orange":"","non lu(s)")+
    (a.elt121?tile("121,5 MHz",a.elt121.active?"SIRÈNE":"veille",a.elt121.active?"red":"green",
      `niveau +${a.elt121.above_db??"—"} dB · ${a.elt121.events_today??0} détection(s) aujourd'hui`):"")+
    (a.listen?tile("Écoute",String((a.listen.config||{}).freq_mhz||"—").replace(".",",")+" MHz","",
      `${((a.listen.config||{}).mod||"").toUpperCase()} · ${a.listen.clients||0} auditeur(s)`+(a.listen.level_db!=null?` · ${a.listen.level_db} dB`:"")):"");
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
function pre(){const v=document.getElementById("lpre").value;if(!v)return;const [f,m]=v.split("|");
  document.getElementById("lf").value=f.replace(".",",");document.getElementById("lm").value=m}
function play(url){const a=document.getElementById("aud");a.style.display="block";a.src=url+"?t="+Date.now();
  a.play().catch(()=>{document.getElementById("linfo").textContent="Appuyez sur ▶ du lecteur pour démarrer le son."})}
async function listen(){const f=document.getElementById("lf").value.replace(",",".");
  if(!f){document.getElementById("linfo").textContent="Choisissez une fréquence.";return}
  document.getElementById("linfo").textContent="Bascule de la clé…";
  try{const r=await fetch("/api/dash/listen",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({freq_mhz:f,mod:document.getElementById("lm").value,squelch:document.getElementById("lsq").value})});
    const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);
    setTimeout(()=>play("http://"+location.hostname+":5014/api/listen/stream.wav"),2500);
    document.getElementById("linfo").textContent="Écoute de "+f.replace(".",",")+" MHz"}
  catch(e){document.getElementById("linfo").textContent="Échec : "+e.message}}
function listenElt(){play("http://"+location.hostname+":5011/api/elt121/stream.wav");
  document.getElementById("linfo").textContent="Écoute de la veille (la détection continue)"}
async function stopListen(){const a=document.getElementById("aud");a.pause();a.removeAttribute("src");a.load();a.style.display="none";
  if((window._d&&window._d.mode||{}).mode==="ecoute"){try{await fetch("/api/dash/listen/stop",{method:"POST"})}catch(e){}}
  document.getElementById("linfo").textContent="Écoute arrêtée.";load()}
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
    if m == "ecoute":
        activity["listen"] = fetch_json("http://127.0.0.1:5014/api/listen/status")
    if m == "elt121":
        e = fetch_json("http://127.0.0.1:5011/api/elt121/status") or {}
        activity["elt121"] = {k: e.get(k) for k in ("active", "above_db", "freq_mhz", "score", "events_today")}
    with _lock:
        alerts = [a for a in _sent if time.time() - a["time"] < 48 * 3600][:10]
    return {"mode": mode, "health": health, "activity": activity, "vigilance": dict(_vigilance), "alerts": alerts}


LISTEN_CONF = "/var/lib/listen/config.json"


def start_listen(data: dict) -> dict:
    """Écoute à distance : règle la fréquence puis donne la clé au module « ecoute »."""
    f = float(str(data.get("freq_mhz", "")).replace(",", "."))
    if not 24 <= f <= 1766:
        raise ValueError("fréquence hors de la plage de la clé (24 à 1766 MHz)")
    cfg = {"freq_mhz": round(f, 5), "mod": "am" if data.get("mod") == "am" else "fm",
           "squelch": max(0, min(500, int(data.get("squelch") or 0)))}
    cur = (fetch_json("http://127.0.0.1:5006/api/sdr/mode") or {}).get("mode")
    if cur == "elt121":
        raise ValueError("la veille 121,5 MHz tient la clé : arrêtez-la d'abord (ou écoutez la veille)")
    if cur == "ecoute":
        req = urllib.request.Request("http://127.0.0.1:5014/api/listen/config", data=json.dumps(cfg).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    try:
        with open(LISTEN_CONF, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        saved = {}
    saved.update(cfg)
    if cur and cur != "off":
        saved["return_mode"] = cur
    os.makedirs(os.path.dirname(LISTEN_CONF), exist_ok=True)
    with open(LISTEN_CONF, "w", encoding="utf-8") as fh:
        json.dump(saved, fh)
    return proxy_sdr_mode("ecoute", auto=True)


def stop_listen() -> dict:
    try:
        with open(LISTEN_CONF, encoding="utf-8") as fh:
            back = json.load(fh).get("return_mode") or "adsb"
    except (OSError, ValueError):
        back = "adsb"
    return proxy_sdr_mode(back, auto=True)


def proxy_sdr_mode(mode: str, auto: bool = False):
    req = urllib.request.Request(f"http://127.0.0.1:5006/api/sdr/mode/{mode}" + ("?auto=1" if auto else ""),
                                 data=b"", method="POST")
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
        elif path == "/api/wx/status":
            self._send(200, wx_status())
        elif path == "/api/monitor/extras":
            self._send(200, {"github": _gh, "space": _space})
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
            if mode not in ("adsb", "ais", "aprs", "openwebrx", "beacon", "elt121", "pocsag", "radiosonde", "meteor",
                            "433", "iss", "ecoute", "off"):
                self._send(400, {"error": "module inconnu"})
                return
            try:
                self._send(200, proxy_sdr_mode(mode))
            except Exception as exc:
                self._send(502, {"error": str(exc)})
        elif path == "/api/wx/config":
            cfg = update_config({"wx": data.get("wx") if isinstance(data.get("wx"), dict) else data})
            if isinstance(data, dict) and data.get("send_now"):
                _wx["last_sent"] = None
            self._send(200, {"ok": True, "config": cfg["wx"]})
        elif path == "/api/dash/listen":
            try:
                self._send(200, start_listen(data if isinstance(data, dict) else {}))
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:
                self._send(502, {"error": str(exc)})
        elif path == "/api/dash/listen/stop":
            try:
                self._send(200, stop_listen())
            except Exception as exc:
                self._send(502, {"error": str(exc)})
        elif path == "/api/monitor/send_document":
            try:
                self._send(200, send_document(data if isinstance(data, dict) else {}))
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
        elif path == "/api/monitor/report":
            title, body = build_report()
            self._send(200, notify(title, body, 2, force=True))
        elif path == "/api/monitor/wefax":
            try:
                self._send(200, save_pc_wefax(data if isinstance(data, dict) else {}))
            except (ValueError, OSError) as exc:
                self._send(400, {"error": str(exc)})
        elif path == "/api/monitor/github/now":
            cfg = load_config()
            try:
                _gh_ensure_branch(cfg)
                data_w = json.dumps(build_week(), ensure_ascii=False, indent=1).encode()
                gh_put(cfg, "semaine/derniere.json", data_w, "Bilan de la semaine (envoi manuel)")
                _gh.update(status="ok", last=time.time())
                self._send(200, {"ok": True})
            except Exception as exc:
                _gh.update(status=f"erreur : {exc}")
                self._send(502, {"error": str(exc)})
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
    threading.Thread(target=wx_loop, daemon=True).start()
    threading.Thread(target=space_loop, daemon=True).start()
    threading.Thread(target=github_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
