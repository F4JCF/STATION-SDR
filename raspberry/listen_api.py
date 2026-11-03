#!/usr/bin/env python3
"""
Écoute à distance : la clé SDR reçoit une fréquence (FM ou AM) et le son est
diffusé en direct au navigateur (tableau de bord du téléphone, PC…), par le
réseau local ou le VPN. Écoute seule. Lancé par la bascule de clé (mode
« ecoute »).

Port LISTEN_PORT (5014) :
  GET  /api/listen/status        fréquence, modulation, niveau, auditeurs
  GET  /api/listen/stream.wav    son en direct (WAV 16 kHz mono, sans fin)
  POST /api/listen/config        {"freq_mhz", "mod" (fm|am), "squelch" (rtl_fm -l, 0 = ouvert),
                                  "gain", "return_mode"}

Sans auditeur pendant 5 minutes, la clé est rendue au module précédent
(« return_mode »), pour que la rotation reprenne.
"""

import json
import logging
import os
import queue
import shutil
import struct
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("LISTEN_PORT", "5014"))
CONF_FILE = os.environ.get("LISTEN_CONF", "/var/lib/listen/config.json")
RATE = 16000
BLOCK = RATE // 10                     # 0,1 s
IDLE_RETURN = 300

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("listen")

conf = {"freq_mhz": 145.5, "mod": "fm", "squelch": 0, "gain": 40, "return_mode": ""}
state = {"running": False, "error": None, "level_db": None, "open": False, "clients": 0,
         "started": time.time(), "last_client": time.time()}
_subs = []
_subs_lock = threading.Lock()
_restart = threading.Event()

try:
    import numpy as np
except ImportError:                    # numpy absent : pas de silencieux, son brut
    np = None


def load():
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            conf.update(json.load(f))
    except (OSError, ValueError):
        pass


def save():
    os.makedirs(os.path.dirname(CONF_FILE), exist_ok=True)
    with open(CONF_FILE, "w", encoding="utf-8") as f:
        json.dump(conf, f)


def wav_header():
    size = 0x7FFFFFF0                  # « sans fin » : le navigateur lit en continu
    return (b"RIFF" + struct.pack("<I", size + 36) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16) + b"data" + struct.pack("<I", size))


def publish(block: bytes):
    with _subs_lock:
        subs = list(_subs)
    for q in subs:
        try:
            q.put_nowait(block)
        except queue.Full:             # auditeur trop lent : on jette le plus ancien
            try:
                q.get_nowait()
                q.put_nowait(block)
            except (queue.Empty, queue.Full):
                pass


def process(raw: bytes) -> bytes:
    """Niveau et gain automatique (AM)."""
    if np is None:
        return raw
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    rms = float(np.sqrt(np.mean(a * a))) + 1e-6
    db = 20 * np.log10(rms / 32768)
    state["level_db"] = round(db, 1)
    state["open"] = True
    if conf.get("mod") == "am":
        a = a - float(np.mean(a))
        a = a * min(20.0, 6000.0 / rms)        # gain automatique pour l'AM
    return np.clip(a, -32767, 32767).astype("<i2").tobytes()


def run():
    while True:
        exe = shutil.which("rtl_fm")
        if not exe:
            state.update(running=False, error="rtl_fm absent (paquet rtl-sdr)")
            time.sleep(30)
            continue
        mod = "am" if conf.get("mod") == "am" else "fm"
        cmd = [exe, "-M", mod, "-f", str(int(float(conf["freq_mhz"]) * 1e6)), "-s", str(RATE),
               "-g", str(conf.get("gain", 40))]
        if int(conf.get("squelch") or 0):
            cmd += ["-l", str(int(conf["squelch"]))]       # silencieux de rtl_fm (niveau radio)
        cmd.append("-")
        if mod == "am":
            cmd[cmd.index("-s"):cmd.index("-s")] = ["-E", "dc"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            state.update(running=False, error=str(exc))
            time.sleep(10)
            continue
        state.update(running=True, error=None)
        log.info("Écoute %.4f MHz %s", float(conf["freq_mhz"]), mod.upper())
        _restart.clear()
        pending = b""
        while not _restart.is_set():
            data = proc.stdout.read(BLOCK * 2)
            if not data:
                break
            pending += data
            while len(pending) >= BLOCK * 2:
                publish(process(pending[:BLOCK * 2]))
                pending = pending[BLOCK * 2:]
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not _restart.is_set():
            state.update(running=False, error="rtl_fm arrêté (clé occupée ?)")
            time.sleep(3)


def idle_watch():
    """Sans auditeur pendant 5 min : rendre la clé au module précédent."""
    while True:
        time.sleep(20)
        if state["clients"] or time.time() - state["last_client"] < IDLE_RETURN:
            continue
        back = conf.get("return_mode") or "adsb"
        try:
            with urllib.request.urlopen("http://127.0.0.1:5006/api/sdr/mode", timeout=10) as r:
                cur = json.loads(r.read().decode()).get("mode")
            if cur == "ecoute":
                log.info("Plus d'auditeur : clé rendue à %s", back)
                req = urllib.request.Request(f"http://127.0.0.1:5006/api/sdr/mode/{back}?auto=1", data=b"",
                                             method="POST")
                urllib.request.urlopen(req, timeout=90).read()
        except Exception as exc:
            log.warning("Retour de la clé impossible : %s", exc)
        state["last_client"] = time.time()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/listen/status":
            self._send(200, {**state, "config": conf})
        elif path in ("/api/listen/stream.wav", "/api/listen/stream"):
            q = queue.Queue(maxsize=30)
            with _subs_lock:
                _subs.append(q)
                state["clients"] = len(_subs)
            state["last_client"] = time.time()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(wav_header())
                while True:
                    try:
                        block = q.get(timeout=0.25)
                    except queue.Empty:
                        block = bytes(BLOCK * 2)          # silencieux fermé : on garde le flux ouvert
                        state["open"] = False
                    self.wfile.write(block)
                    state["last_client"] = time.time()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with _subs_lock:
                    if q in _subs:
                        _subs.remove(q)
                    state["clients"] = len(_subs)
        else:
            self._send(404, {"error": "introuvable"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path != "/api/listen/config":
            self._send(404, {"error": "introuvable"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            restart = False
            if "freq_mhz" in body:
                f = float(str(body["freq_mhz"]).replace(",", "."))
                if not 24 <= f <= 1766:
                    raise ValueError("fréquence hors de la plage de la clé (24 à 1766 MHz)")
                restart |= f != float(conf["freq_mhz"])
                conf["freq_mhz"] = round(f, 5)
            if "mod" in body:
                m = "am" if str(body["mod"]).lower() == "am" else "fm"
                restart |= m != conf["mod"]
                conf["mod"] = m
            if "gain" in body:
                g = max(0.0, min(49.6, float(body["gain"])))
                restart |= g != float(conf["gain"])
                conf["gain"] = g
            if "squelch" in body:
                sq = max(0, min(500, int(body["squelch"])))
                restart |= sq != int(conf.get("squelch") or 0)
                conf["squelch"] = sq
            if body.get("return_mode"):
                conf["return_mode"] = str(body["return_mode"])
        except (ValueError, TypeError) as exc:
            self._send(400, {"error": str(exc)})
            return
        save()
        state["last_client"] = time.time()
        if restart:
            _restart.set()
        self._send(200, {"ok": True, "config": conf})

    def log_message(self, *_a):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    load()
    threading.Thread(target=run, daemon=True).start()
    threading.Thread(target=idle_watch, daemon=True).start()
    Server(("0.0.0.0", PORT), Handler).serve_forever()
