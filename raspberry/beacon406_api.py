#!/usr/bin/env python3
"""
Réception et décodage des balises de détresse 406 MHz (COSPAS-SARSAT)

Lance le décodeur dec406 (Fabrice Morel, licence MIT) sur la clé RTL-SDR,
balaie 406,0–406,1 MHz, et expose en JSON les salves détectées et les
trames décodées (balises de 1re et 2e génération : ELT, EPIRB, PLB).

Endpoints (port BEACON406_PORT, 5009) :
  GET  /api/beacon/status              état du décodeur, compteurs
  GET  /api/beacon/frames?limit=200    trames décodées (plus récentes d'abord)
  GET  /api/beacon/bursts              dernières salves détectées
  GET  /api/beacon/decode?hex=...      décodage manuel d'une trame hexadécimale
  POST /api/beacon/clear               efface l'historique

Ce service ne fait qu'ÉCOUTER : il n'émet rien.
"""

import collections
import json
import logging
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("BEACON406_PORT", "5009"))
DEC_DIR = os.environ.get("DEC406_DIR", "/opt/dec406")
SCAN_BIN = os.path.join(DEC_DIR, "dec406_scan")
HEX_BIN = os.path.join(DEC_DIR, "dec406_hex")
F_LOW = os.environ.get("BEACON406_FLOW", "406.0M")
F_HIGH = os.environ.get("BEACON406_FHIGH", "406.1M")
GAIN = os.environ.get("BEACON406_GAIN", "-1")      # -1 = gain automatique
PPM = os.environ.get("BEACON406_PPM", "0")
STORE = os.environ.get("BEACON406_STORE", "/var/lib/beacon406/frames.jsonl")
STATION = (float(os.environ.get("STATION_LAT", "43.590541")), float(os.environ.get("STATION_LON", "7.002072")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("beacon406")
_lock = threading.Lock()
frames = collections.deque(maxlen=500)
bursts = collections.deque(maxlen=200)
state = {"running": False, "started_at": None, "last_line_at": None, "bursts": 0, "decoded": 0,
         "failed": 0, "error": None, "band": f"{F_LOW}–{F_HIGH}"}

BURST_RE = re.compile(
    r"\[(?P<t>[\d:.]+)\]\s+BURST\s+(?P<f>[\d.]+)\s*MHz\s+BW\s*~\s*(?P<bw>[\d.]+)\s*kHz\s+(?P<type>\w+)\s+"
    r"SNR\s*(?P<snr>-?[\d.]+)\s*dB\s+dur\s*(?P<dur>[\d.]+)\s*s\s+dt\s*(?P<dt>[\d.]+)")
POS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*°?\s*([NS])\s*,\s*(\d+(?:\.\d+)?)\s*°?\s*([EW])")


def haversine_km(lat1, lon1, lat2, lon2):
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * r * math.asin(math.sqrt(a))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return d, (math.degrees(math.atan2(y, x)) + 360) % 360


def parse_block(text: str) -> dict:
    """Transforme le texte de dec406 en champs exploitables."""
    fields = {}
    for line in text.splitlines():
        m = re.match(r"\s*([^:\[\]=]{2,40}?)\s*:\s+(.+)$", line)
        if m and not line.strip().startswith("http"):
            key = m.group(1).strip()
            fields.setdefault(key, m.group(2).strip())
    gen = "2G" if "SECOND GENERATION" in text else "1G"
    out = {"generation": gen, "fields": fields, "text": text.strip()}
    out["hex_id"] = (fields.get("Hex ID") or fields.get("23 Hex ID") or "").split()[0] if (
        fields.get("Hex ID") or fields.get("23 Hex ID")) else None
    out["protocol"] = fields.get("Protocol") or fields.get("Type")
    out["country"] = fields.get("Country") or fields.get("Country Code")
    out["identification"] = fields.get("Identification") or fields.get("Vessel ID") or fields.get("Serial Number")
    low = text.lower()
    out["test"] = bool(re.search(r"test protocol:\s*active", low) or "self-test" in low or "self test" in low
                       or "orbitography" in low or "frame mode: self" in low)
    lat = lon = None
    for key in ("Composite position", "Coordinates", "Position (PDF-1)", "Position"):
        val = fields.get(key)
        if not val or "no location" in val.lower() or "default" in val.lower():
            continue
        m = POS_RE.search(val)
        if m:
            la, lo = float(m.group(1)), float(m.group(3))
            la = -la if m.group(2) == "S" else la
            lo = -lo if m.group(4) == "W" else lo
            if -90 <= la <= 90 and -180 <= lo <= 180:
                lat, lon = la, lo
                break
    out["lat"], out["lon"] = lat, lon
    if lat is not None:
        d, b = haversine_km(STATION[0], STATION[1], lat, lon)
        out["distance_km"], out["bearing_deg"] = round(d, 1), round(b)
    return out


def store(frame: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        with open(STORE, "a", encoding="utf-8") as f:
            f.write(json.dumps(frame, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_store() -> None:
    try:
        with open(STORE, "r", encoding="utf-8") as f:
            for line in f.readlines()[-500:]:
                try:
                    frames.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass


def run_scanner() -> None:
    """Lance dec406_scan et lit sa sortie en continu (redémarre s'il s'arrête)."""
    while True:
        if not os.path.exists(SCAN_BIN):
            state.update(running=False, error=f"décodeur absent ({SCAN_BIN}) : lancer install_beacon406.sh")
            time.sleep(30)
            continue
        env = dict(os.environ, DEC406_BACKEND="rtl", DSSS_DIAG="0", RTL_DIAG="0", DUMP_FAIL="0", DUMP_OK="0")
        try:
            proc = subprocess.Popen([SCAN_BIN, F_LOW, F_HIGH, GAIN, PPM], cwd=DEC_DIR, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as exc:
            state.update(running=False, error=str(exc))
            time.sleep(30)
            continue
        state.update(running=True, started_at=time.time(), error=None)
        log.info("dec406_scan lancé (%s–%s, gain %s, ppm %s)", F_LOW, F_HIGH, GAIN, PPM)
        err_tail = collections.deque(maxlen=20)

        def read_err():
            for line in proc.stderr:
                line = line.rstrip()
                if line and not line.startswith("<7>"):
                    err_tail.append(line)
        threading.Thread(target=read_err, daemon=True).start()

        block, gen, last_burst = None, None, None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            state["last_line_at"] = time.time()
            if block is None:
                m = BURST_RE.search(line)
                if m:
                    last_burst = {"time": time.time(), "freq_mhz": float(m.group("f")), "bw_khz": float(m.group("bw")),
                                  "type": m.group("type"), "snr_db": float(m.group("snr")),
                                  "dur_s": float(m.group("dur")), "status": "reçue"}
                    with _lock:
                        bursts.appendleft(last_burst)
                        state["bursts"] += 1
                    continue
                if "BCH FAIL" in line or "decode failed" in line or "no frame decoded" in line:
                    if last_burst:
                        last_burst["status"] = "non décodée"
                    state["failed"] += 1
                    continue
                if "=== 406 MHz BEACON DECODE" in line or "=== 406 MHz SECOND GENERATION" in line:
                    block, gen = [line], ("2G" if "SECOND" in line else "1G")
                continue
            # dans un bloc de décodage
            end = (gen == "1G" and not line.strip()) or (gen == "2G" and re.fullmatch(r"=+", line.strip() or "x"))
            if not end:
                block.append(line)
                continue
            frame = parse_block("\n".join(block))
            frame["time"] = time.time()
            if last_burst and time.time() - last_burst["time"] < 30:
                frame.update(freq_mhz=last_burst["freq_mhz"], snr_db=last_burst["snr_db"], burst_type=last_burst["type"])
                last_burst["status"] = "décodée"
                last_burst["hex_id"] = frame.get("hex_id")
            with _lock:
                frames.appendleft(frame)
                state["decoded"] += 1
            store(frame)
            log.info("Trame décodée : %s %s %s", frame.get("hex_id"), frame.get("protocol"),
                     "TEST" if frame.get("test") else "")
            block = None
        code = proc.wait()
        state.update(running=False, error=f"décodeur arrêté (code {code})" + (
            f" : {err_tail[-1]}" if err_tail else ""))
        log.warning("dec406_scan arrêté (code %s) : %s", code, " | ".join(err_tail))
        time.sleep(10)


def manual_decode(hex_str: str) -> dict:
    if not re.fullmatch(r"[0-9A-Fa-f]{15,70}", hex_str or ""):
        return {"error": "trame hexadécimale invalide (15 à 70 caractères 0-9 A-F)"}
    if not os.path.exists(HEX_BIN):
        return {"error": "dec406_hex absent : lancer install_beacon406.sh"}
    try:
        res = subprocess.run([HEX_BIN, hex_str.upper()], capture_output=True, text=True, timeout=20, cwd=DEC_DIR)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}
    text = "\n".join(l for l in (res.stdout + "\n" + res.stderr).splitlines()
                     if not l.startswith("<7>") and "decoding completed" not in l and "Starting" not in l)
    if "=== 406 MHz" not in text:
        return {"error": "aucune trame valide reconnue", "text": text.strip()}
    out = parse_block(text[text.index("=== 406 MHz"):])
    out["time"] = time.time()
    out["manual"] = True
    return out


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
        q = parse_qs(u.query)
        path = u.path.rstrip("/")
        if path == "/api/beacon/status":
            hour_ago = time.time() - 3600
            with _lock:
                last = frames[0] if frames else None
                self._send(200, {**state, "frames": len(frames),
                                 "bursts_last_hour": sum(1 for b in bursts if b["time"] > hour_ago),
                                 "last_frame": last, "last_burst": bursts[0] if bursts else None,
                                 "station": STATION})
        elif path == "/api/beacon/frames":
            try:
                limit = max(1, min(500, int((q.get("limit") or ["200"])[0])))
            except ValueError:
                limit = 200
            with _lock:
                self._send(200, list(frames)[:limit])
        elif path == "/api/beacon/bursts":
            with _lock:
                self._send(200, list(bursts))
        elif path == "/api/beacon/decode":
            self._send(200, manual_decode((q.get("hex") or [""])[0].strip()))
        else:
            self._send(404, {"error": "introuvable"})

    def do_POST(self):
        if urlparse(self.path).path.rstrip("/") == "/api/beacon/clear":
            with _lock:
                frames.clear()
                bursts.clear()
                state.update(bursts=0, decoded=0, failed=0)
            try:
                os.remove(STORE)
            except OSError:
                pass
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "introuvable"})

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    load_store()
    threading.Thread(target=run_scanner, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
