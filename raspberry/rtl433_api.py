#!/usr/bin/env python3
"""
Capteurs 433 MHz (rtl_433) : stations météo, sondes de température,
compteurs, télécommandes... reçus par la clé SDR.

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
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("RTL433_PORT", "5010"))
FREQ = os.environ.get("RTL433_FREQ", "433.92M")
GAIN = os.environ.get("RTL433_GAIN", "0")          # 0 = automatique
STORE = os.environ.get("RTL433_STORE", "/var/lib/rtl433/devices.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rtl433")
_lock = threading.Lock()
devices = {}
events = collections.deque(maxlen=300)
state = {"running": False, "error": None, "started_at": None, "messages": 0, "freq": FREQ}

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


def run():
    exe = shutil.which("rtl_433")
    last_save = 0
    while True:
        if not exe:
            exe = shutil.which("rtl_433")
            state.update(running=False, error="rtl_433 absent : lancer install_433.sh")
            time.sleep(30)
            continue
        cmd = [exe, "-f", FREQ, "-F", "json", "-M", "time:unix", "-M", "level", "-M", "protocol"]
        if GAIN and GAIN != "0":
            cmd += ["-g", GAIN]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as exc:
            state.update(running=False, error=str(exc))
            time.sleep(30)
            continue
        state.update(running=True, error=None, started_at=time.time())
        log.info("rtl_433 lancé sur %s", FREQ)
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
