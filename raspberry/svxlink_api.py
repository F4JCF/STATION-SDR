#!/usr/bin/env python3
"""
API JSON de l'activité SvxLink (F4JCF-SDR-STATION)

Suit le journal de SvxLink (/var/log/svxlink) en continu et en déduit
l'état en direct : émetteur, squelch, module actif, stations EchoLink
connectées, derniers événements et avertissements.

Endpoint :
  GET /api/svxlink/status

Bibliothèque standard uniquement (tourne en root pour lire le journal).
Variables : SVXLINK_LOG (/var/log/svxlink), SVXLINK_CONF
(/etc/svxlink/svxlink.conf), SVXLINK_API_PORT (5007).
"""

import collections
import configparser
import json
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_PATH = os.environ.get("SVXLINK_LOG", "/var/log/svxlink")
CONF_PATH = os.environ.get("SVXLINK_CONF", "/etc/svxlink/svxlink.conf")
PORT = int(os.environ.get("SVXLINK_API_PORT", "5007"))
INITIAL_READ_BYTES = 2 * 1024 * 1024

LINE_RE = re.compile(r"^(\w{3} \w{3} +\d+ \d\d:\d\d:\d\d \d{4}): (.*)$")
CALL = r"([A-Z0-9]{1,3}[0-9][A-Z0-9/\-]*)"

lock = threading.Lock()
state = {
    "tx": False, "tx_since": None, "last_tx": None,
    "squelch": False, "squelch_since": None, "last_rx": None,
    "active_module": None,
    "echolink_directory": None,
    "connected": {},            # indicatif -> horodatage de connexion
    "last_station": None, "last_station_at": None,
    "distortion_at": None, "distortion_count": 0,
    "missing_audio": set(),
    "started_at": None,
}
events = collections.deque(maxlen=60)


def _ts(text: str) -> float:
    try:
        return datetime.strptime(" ".join(text.split()), "%a %b %d %H:%M:%S %Y").timestamp()
    except ValueError:
        return time.time()


def _event(t: float, kind: str, text: str) -> None:
    events.append({"time": t, "kind": kind, "text": text})


def parse_line(line: str) -> None:
    m = LINE_RE.match(line.rstrip("\n"))
    if not m:
        return
    t, msg = _ts(m.group(1)), m.group(2).strip()
    s = state
    if "Turning the transmitter ON" in msg:
        if not s["tx"]:
            s["tx"], s["tx_since"] = True, t
    elif "Turning the transmitter OFF" in msg:
        if s["tx"]:
            s["last_tx"] = {"start": s["tx_since"], "end": t}
            _event(t, "tx", f"Émission ({max(0, int(t - (s['tx_since'] or t)))} s)")
        s["tx"], s["tx_since"] = False, None
    elif "The squelch is OPEN" in msg:
        if not s["squelch"]:
            s["squelch"], s["squelch_since"] = True, t
    elif "The squelch is CLOSED" in msg:
        if s["squelch"]:
            s["last_rx"] = {"start": s["squelch_since"], "end": t}
            _event(t, "rx", f"Réception ({max(0, int(t - (s['squelch_since'] or t)))} s)")
        s["squelch"], s["squelch_since"] = False, None
    elif m2 := re.search(r"Activating module (\w+)", msg):
        s["active_module"] = m2.group(1)
        _event(t, "module", f"Module {m2.group(1)} activé")
    elif m2 := re.search(r"Deactivating module (\w+)", msg):
        if s["active_module"] == m2.group(1):
            s["active_module"] = None
        _event(t, "module", f"Module {m2.group(1)} désactivé")
    elif m2 := re.search(r"EchoLink directory status changed to (\w+)", msg):
        s["echolink_directory"] = m2.group(1).upper()
        _event(t, "echolink", f"Annuaire EchoLink : {m2.group(1).upper()}")
    elif m2 := re.search(rf"Incoming EchoLink connection from {CALL}", msg):
        _event(t, "echolink", f"Connexion entrante de {m2.group(1)}")
    elif m2 := re.search(rf"^{CALL}: EchoLink QSO state changed to (\w+)", msg):
        call, st = m2.group(1), m2.group(2).upper()
        if st == "CONNECTED":
            s["connected"][call] = t
            s["last_station"], s["last_station_at"] = call, t
            _event(t, "echolink", f"{call} connecté")
        elif st == "DISCONNECTED":
            s["connected"].pop(call, None)
            _event(t, "echolink", f"{call} déconnecté")
    elif m2 := re.search(rf"chat message received from {CALL}", msg):
        s["last_station"], s["last_station_at"] = m2.group(1), t
        _event(t, "echolink", f"Message de {m2.group(1)}")
    elif "Distortion detected" in msg:
        s["distortion_at"] = t
        s["distortion_count"] += 1
    elif m2 := re.search(r"Could not find audio clip '([^']+)'", msg):
        s["missing_audio"].add(m2.group(1))
    elif "Initialization done" in msg:
        # Redémarrage de SvxLink : l'état en cours repart de zéro.
        s.update(tx=False, tx_since=None, squelch=False, squelch_since=None,
                 active_module=None, started_at=t)
        s["connected"].clear()
        _event(t, "system", "SvxLink démarré")
    elif m2 := re.search(r"digit=(\S+)", msg):
        _event(t, "dtmf", f"DTMF {m2.group(1)}")
    elif "Sending long identification" in msg or "Sending short identification" in msg:
        _event(t, "ident", "Identification")


def follow_log() -> None:
    """Lit la fin du journal pour reconstituer l'état, puis le suit comme
    `tail -F` (gère la rotation du fichier)."""
    pos, inode = None, None
    while True:
        try:
            st = os.stat(LOG_PATH)
            skip_partial = False
            if pos is None:
                inode, pos = st.st_ino, max(0, st.st_size - INITIAL_READ_BYTES)
                skip_partial = pos > 0
            elif st.st_ino != inode or st.st_size < pos:
                inode, pos = st.st_ino, 0  # fichier tourné ou tronqué
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                if skip_partial:
                    f.readline()  # première ligne probablement coupée
                chunk = f.readlines()
                pos = f.tell()
            if chunk:
                with lock:
                    for line in chunk:
                        parse_line(line)
        except OSError:
            pass
        time.sleep(1)


def read_config() -> dict:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    try:
        cp.read(CONF_PATH, encoding="utf-8")
    except (configparser.Error, OSError):
        return {}
    logics = [x.strip() for x in cp.get("GLOBAL", "LOGICS", fallback="").split(",") if x.strip()]
    first = logics[0] if logics else None
    return {
        "logics": logics,
        "callsign": cp.get(first, "CALLSIGN", fallback=None) if first else None,
        "modules": [x.strip().replace("Module", "", 1)
                    for x in cp.get(first, "MODULES", fallback="").split(",") if x.strip()] if first else [],
    }


def status() -> dict:
    with lock:
        s = state
        return {
            **read_config(),
            "log_found": os.path.exists(LOG_PATH),
            "tx": s["tx"], "tx_since": s["tx_since"], "last_tx": s["last_tx"],
            "squelch": s["squelch"], "squelch_since": s["squelch_since"], "last_rx": s["last_rx"],
            "active_module": s["active_module"],
            "echolink_directory": s["echolink_directory"],
            "connected": [{"callsign": c, "since": t} for c, t in sorted(s["connected"].items(), key=lambda x: x[1])],
            "last_station": s["last_station"], "last_station_at": s["last_station_at"],
            "distortion_at": s["distortion_at"], "distortion_count": s["distortion_count"],
            "missing_audio_count": len(s["missing_audio"]),
            "started_at": s["started_at"],
            "events": list(reversed(events)),
            "now": time.time(),
        }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") != "/api/svxlink/status":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(status(), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    threading.Thread(target=follow_log, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
