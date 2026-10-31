#!/usr/bin/env python3
"""
Réception POCSAG (messagerie / radiomessagerie) : rtl_fm (FM étroite) puis
multimon-ng (POCSAG 512, 1200 et 2400 bauds). Par défaut DAPNET, le réseau
de radiomessagerie radioamateur, sur 439,9875 MHz.

Port POCSAG_PORT (5012) :
  GET  /api/pocsag/status              état, fréquence, compteurs
  GET  /api/pocsag/messages?limit=300  derniers messages (plus récents d'abord)
  POST /api/pocsag/config              {"freq_mhz", "gain", "ppm", "watch": [...], "hide_tech"}
  POST /api/pocsag/clear               efface la liste
Écoute seule. Les messages restent en mémoire (rien n'est écrit sur la
carte SD) et sont perdus à l'arrêt du service.
"""

import collections
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("POCSAG_PORT", "5012"))
DATA = os.environ.get("POCSAG_DIR", "/var/lib/pocsag")
CONF_FILE = os.path.join(DATA, "config.json")
RATE = 22050

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pocsag")
_lock = threading.Lock()

conf = {"freq_mhz": 439.9875, "gain": 40.0, "ppm": 0, "watch": ["F4JCF"], "hide_tech": True}
state = {"running": False, "error": None, "started_at": None, "messages": 0, "last_time": None}
messages = collections.deque(maxlen=1000)
_restart = threading.Event()
_procs = []

LINE_RE = re.compile(r"^(POCSAG(?:512|1200|2400)):\s*Address:\s*(\d+)\s+Function:\s*(\d)"
                     r"(?:\s+(Alpha|Numeric|Skyper):\s?(.*))?\s*$")
# DAPNET : diffusion de l'heure et rubriques d'information, peu utiles en liste
TECH_RICS = {2504, 224, 200, 208, 216, 4512, 4520}


def load():
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            conf.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_conf():
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(CONF_FILE, "w", encoding="utf-8") as f:
            json.dump(conf, f, ensure_ascii=False)
    except OSError:
        pass


def clean_text(s: str) -> str:
    s = re.sub(r"<(NUL|EOT|ETX|ETB|ENQ|ACK|BEL|BS|LF|CR|DLE|DC\d|NAK|SYN|CAN|EM|SUB|ESC|FS|GS|RS|US|DEL)>",
               lambda m: "\n" if m.group(1) in ("LF", "CR") else "", s)
    return s.strip()


def parse_line(line: str):
    m = LINE_RE.match(line.strip())
    if not m:
        return None
    ric = int(m.group(2))
    kind = m.group(4) or "Tonalité"
    text = clean_text(m.group(5) or "")
    return {"rate": m.group(1).replace("POCSAG", ""), "ric": ric, "function": int(m.group(3)),
            "type": {"Alpha": "Texte", "Numeric": "Numérique", "Skyper": "Skyper"}.get(kind, kind),
            "text": text, "tech": ric in TECH_RICS or text.startswith("XTIME=")}


def watched(msg) -> bool:
    hay = f"{msg['ric']} {msg['text']}".upper()
    return any(w and w.upper() in hay for w in conf.get("watch") or [])


def stop_procs():
    for p in _procs:
        try:
            p.kill()
        except OSError:
            pass
    _procs.clear()


def run():
    while True:
        rtl, mm = shutil.which("rtl_fm"), shutil.which("multimon-ng")
        if not rtl or not mm:
            state.update(running=False, error="rtl_fm ou multimon-ng absent : lancer install_pocsag.sh")
            time.sleep(30)
            continue
        cmd1 = [rtl, "-d", "0", "-M", "fm", "-f", f"{conf['freq_mhz']}M", "-s", str(RATE),
                "-g", str(conf["gain"]), "-p", str(int(conf.get("ppm") or 0)), "-l", "0", "-"]
        cmd2 = [mm, "-t", "raw", "-a", "POCSAG512", "-a", "POCSAG1200", "-a", "POCSAG2400", "-q", "-u", "-"]
        try:
            p1 = subprocess.Popen(cmd1, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p2 = subprocess.Popen(cmd2, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, bufsize=1)
            p1.stdout.close()
        except OSError as exc:
            state.update(running=False, error=str(exc))
            time.sleep(20)
            continue
        _procs[:] = [p1, p2]
        err = collections.deque(maxlen=6)
        threading.Thread(target=lambda: [err.append(l.decode("utf-8", "replace").rstrip()) for l in p1.stderr],
                         daemon=True).start()
        _restart.clear()
        state.update(running=True, error=None, started_at=time.time())
        log.info("Écoute POCSAG sur %.4f MHz", conf["freq_mhz"])
        for line in p2.stdout:
            msg = parse_line(line)
            if msg is None or (not msg["text"] and msg["ric"] < 8):
                continue  # adresses 0 à 7 sans contenu : faux décodages dans le bruit
            msg["time"] = time.time()
            msg["freq_mhz"] = conf["freq_mhz"]
            msg["watch"] = watched(msg)
            with _lock:
                messages.appendleft(msg)
                state["messages"] += 1
                state["last_time"] = msg["time"]
            if msg["watch"]:
                log.info("Message surveillé RIC %s : %s", msg["ric"], msg["text"][:80])
        stop_procs()
        p1.wait()
        if _restart.is_set():
            continue
        code = p1.returncode
        state.update(running=False, error=f"rtl_fm arrêté (code {code})" + (f" : {err[-1]}" if err else ""))
        log.warning("Arrêt du décodage (code %s) : %s", code, " | ".join(err))
        time.sleep(8)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path.rstrip("/"), parse_qs(u.query)
        if path == "/api/pocsag/status":
            hour = time.time() - 3600
            with _lock:
                last_hour = sum(1 for m in messages if m["time"] >= hour and not m["tech"])
                watch_count = sum(1 for m in messages if m["watch"])
            self._send(200, {**state, **conf, "last_hour": last_hour, "watched": watch_count})
        elif path == "/api/pocsag/messages":
            limit = int(q.get("limit", ["300"])[0])
            hide = conf.get("hide_tech", True)
            with _lock:
                out = [m for m in messages if not (hide and m["tech"])][:limit]
            self._send(200, out)
        else:
            self._send(404, {"error": "introuvable"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/pocsag/config":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                restart = False
                if "freq_mhz" in body:
                    f = round(float(body["freq_mhz"]), 4)
                    if not 24 <= f <= 1700:
                        raise ValueError("fréquence hors de la plage de la clé")
                    restart |= f != conf["freq_mhz"]
                    conf["freq_mhz"] = f
                if "gain" in body:
                    g = max(0.0, min(49.6, float(body["gain"])))
                    restart |= g != conf["gain"]
                    conf["gain"] = g
                if "ppm" in body:
                    p = int(body["ppm"])
                    restart |= p != conf.get("ppm")
                    conf["ppm"] = p
                if "watch" in body:
                    conf["watch"] = [str(w).strip() for w in body["watch"] if str(w).strip()][:20]
                if "hide_tech" in body:
                    conf["hide_tech"] = bool(body["hide_tech"])
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": str(exc)})
                return
            save_conf()
            with _lock:
                for m in messages:
                    m["watch"] = watched(m)
            if restart:
                _restart.set()
                stop_procs()
            self._send(200, {"ok": True, **conf})
        elif path == "/api/pocsag/clear":
            with _lock:
                messages.clear()
                state["messages"] = 0
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "introuvable"})

    def log_message(self, *_a):
        pass


if __name__ == "__main__":
    load()
    threading.Thread(target=run, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
