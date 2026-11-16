#!/usr/bin/env python3
"""
Capteurs 433 / 868 MHz (rtl_433) : stations météo (Oregon, LaCrosse,
Bresser, Fine Offset...), sondes de température, compteurs... reçus par la
clé SDR. Avec les deux bandes, rtl_433 alterne 433,92 et 868,3 MHz
(réglage dans BANDS_FILE, écrit par le service de surveillance).

Lance rtl_433 (sortie JSON) et expose (port RTL433_PORT, 5010) :
  GET  /api/433/status    état du décodeur, compteurs
  GET  /api/433/devices   un enregistrement par capteur (dernières valeurs)
  GET  /api/433/events    derniers messages bruts
  POST /api/433/clear     efface l'historique
Écoute seule.
"""

import collections
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("RTL433_PORT", "5010"))
FREQ = os.environ.get("RTL433_FREQ", "433.92M")
GAIN = os.environ.get("RTL433_GAIN", "0")          # 0 = automatique
STORE = os.environ.get("RTL433_STORE", "/var/lib/rtl433/devices.json")
BANDS_FILE = os.environ.get("RTL433_BANDS", "/var/lib/rtl433/bands.json")
BAND_FREQ = {"433": "433.92M", "868": "868.3M"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rtl433")
_lock = threading.Lock()
devices = {}
events = collections.deque(maxlen=300)
state = {"running": False, "error": None, "started_at": None, "messages": 0, "freq": FREQ, "bands": [], "hop": 0}

# Champs « techniques » à ne pas présenter comme des mesures
META = {"time", "model", "id", "channel", "subtype", "mic", "mod", "freq", "freq1", "freq2", "rssi", "snr",
        "noise", "protocol", "battery_ok", "battery_mV"}


def device_key(msg):
    return "|".join(str(msg.get(k, "")) for k in ("model", "id", "channel", "subtype"))


def save():
    try:
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        tmp = STORE + ".tmp"
        with _lock:
            data = dict(devices)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, STORE)
    except OSError:
        pass


def load():
    try:
        with open(STORE, "r", encoding="utf-8") as f:
            devices.update(json.load(f))
    except (OSError, ValueError):
        pass


def bands_conf():
    """Bandes écoutées et durée de chaque palier d'alternance (s)."""
    conf = {"bands": ["433", "868"], "hop": 30}
    try:
        with open(BANDS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        b = [x for x in data.get("bands") or [] if x in BAND_FREQ]
        if b:
            conf["bands"] = b
        conf["hop"] = max(10, min(600, int(data.get("hop") or 30)))
    except (OSError, ValueError, TypeError):
        pass
    return conf


def _mtime():
    try:
        return os.path.getmtime(BANDS_FILE)
    except OSError:
        return 0


def band_of(msg, current):
    f = msg.get("freq")
    if isinstance(f, (int, float)) and f > 0:
        return "868" if f > 600 else "433"
    return current


def run():
    exe = shutil.which("rtl_433")
    last_save = 0
    while True:
        if not exe:
            exe = shutil.which("rtl_433")
            state.update(running=False, error="rtl_433 absent : lancer install_433.sh")
            time.sleep(30)
            continue
        conf = bands_conf()
        mtime = _mtime()
        cmd = [exe]
        for b in conf["bands"]:
            cmd += ["-f", BAND_FREQ[b]]
        if len(conf["bands"]) > 1:
            cmd += ["-H", str(conf["hop"])]
        freq_txt = " + ".join(BAND_FREQ[b].replace("M", "").replace(".", ",") for b in conf["bands"]) + " MHz"
        cmd += ["-F", "json", "-M", "time:unix", "-M", "level", "-M", "protocol"]
        if GAIN and GAIN != "0":
            cmd += ["-g", GAIN]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                    start_new_session=True)
        except OSError as exc:
            state.update(running=False, error=str(exc))
            time.sleep(30)
            continue
        state.update(running=True, error=None, started_at=time.time(), freq=freq_txt,
                     bands=conf["bands"], hop=conf["hop"])
        log.info("rtl_433 lancé : %s", " ".join(cmd[1:]))

        def watch_conf(p=proc, m=mtime):
            # réglage des bandes modifié : on relance rtl_433
            while p.poll() is None:
                time.sleep(10)
                if _mtime() != m:
                    log.info("bandes modifiées : redémarrage de rtl_433")
                    try:
                        os.killpg(p.pid, signal.SIGTERM)
                        time.sleep(3)
                        if p.poll() is None:
                            os.killpg(p.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    return
        threading.Thread(target=watch_conf, daemon=True).start()
        default_band = conf["bands"][0]
        err = collections.deque(maxlen=10)
        threading.Thread(target=lambda: [err.append(l.rstrip()) for l in proc.stderr], daemon=True).start()
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            now = time.time()
            msg["_rx"] = now
            key = device_key(msg)
            with _lock:
                state["messages"] += 1
                events.appendleft(msg)
                d = devices.get(key) or {"key": key, "model": msg.get("model"), "id": msg.get("id"),
                                         "channel": msg.get("channel"), "first_seen": now, "count": 0, "values": {}}
                d["count"] += 1
                d["last_seen"] = now
                d["band"] = band_of(msg, default_band if len(conf["bands"]) == 1 else d.get("band", default_band))
                for k in ("rssi", "snr", "battery_ok", "freq"):
                    if k in msg:
                        d[k] = msg[k]
                for k, v in msg.items():
                    if k not in META and not k.startswith("_") and isinstance(v, (int, float, str)):
                        d["values"][k] = v
                devices[key] = d
            if now - last_save > 60:
                save()
                last_save = now
        code = proc.wait()
        if _mtime() != mtime:
            state.update(running=False, error=None)
            time.sleep(2)
            continue
        state.update(running=False, error=f"rtl_433 arrêté (code {code})" + (f" : {err[-1]}" if err else ""))
        log.warning("rtl_433 arrêté (code %s) : %s", code, " | ".join(err))
        time.sleep(10)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        with _lock:
            if path == "/api/433/status":
                hour = time.time() - 3600
                self._send(200, {**state, "devices": len(devices),
                                 "active_last_hour": sum(1 for d in devices.values() if d.get("last_seen", 0) > hour)})
            elif path == "/api/433/devices":
                self._send(200, sorted(devices.values(), key=lambda d: -d.get("last_seen", 0)))
            elif path == "/api/433/events":
                self._send(200, list(events)[:200])
            else:
                self._send(404, {"error": "introuvable"})

    def do_POST(self):
        if urlparse(self.path).path.rstrip("/") == "/api/433/clear":
            with _lock:
                devices.clear()
                events.clear()
                state["messages"] = 0
            save()
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "introuvable"})

    def log_message(self, *_a):
        pass


if __name__ == "__main__":
    load()
    threading.Thread(target=run, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
