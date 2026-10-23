#!/usr/bin/env python3
"""
Veille 121,5 MHz : reconnaissance de la sirène des balises de détresse
(ELT avion, PLB, EPIRB, et radioralliement des balises 406).

Le 121,5 MHz ne transporte aucune donnée : c'est une porteuse AM modulée
par une tonalité qui balaie (en descendant) de ~1600 à ~300 Hz, 2 à 4 fois
par seconde. Ce service écoute la fréquence en AM avec rtl_fm, mesure le
niveau du signal, reconnaît la sirène dans le spectre audio et enregistre
quelques secondes d'audio à chaque détection. Écoute seule.

Port ELT121_PORT (5011) :
  GET  /api/elt121/status              état, niveau, détection en cours
  GET  /api/elt121/level?seconds=900   historique du niveau (1 point / s)
  GET  /api/elt121/waterfall           spectrogramme audio des 6 dernières s
  GET  /api/elt121/events              détections (plus récentes d'abord)
  GET  /api/elt121/audio?seconds=10    dernières secondes reçues (WAV)
  GET  /api/elt121/rec/<nom>.wav       enregistrement d'une détection
  POST /api/elt121/config              {"freq_mhz", "gain", "ppm"}
  POST /api/elt121/clear               efface les détections
"""

import collections
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

PORT = int(os.environ.get("ELT121_PORT", "5011"))
DATA = os.environ.get("ELT121_DIR", "/var/lib/elt121")
REC_DIR = os.path.join(DATA, "rec")
CONF_FILE = os.path.join(DATA, "config.json")
EVENTS_FILE = os.path.join(DATA, "events.json")

RF_RATE = 24000          # sortie de rtl_fm (AM)
DEC = 4                  # -> audio à 6 kHz
AUDIO_RATE = RF_RATE // DEC
NFFT, HOP = 256, 128     # 43 ms, pas de 21 ms
WIN = np.hanning(NFFT).astype(np.float32)
BIN_HZ = AUDIO_RATE / NFFT
WF_LO, WF_HI = 4, 124    # 94 Hz – 2,9 kHz pour le spectrogramme
WF_GROUP = 2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("elt121")
_lock = threading.Lock()

conf = {"freq_mhz": 121.5, "gain": 40.0, "ppm": 0}
state = {"running": False, "error": None, "started_at": None, "active": False, "active_since": None,
         "level_db": None, "floor_db": None, "above_db": None, "tonal": 0.0, "span_hz": 0, "sweeps_per_s": 0.0,
         "direction": None, "score": 0.0, "regular": 0.0}
levels = collections.deque(maxlen=4 * 3600)       # (t, dB) toutes les secondes
quiet = collections.deque(maxlen=1800)            # niveaux hors sirène (bruit de fond)
audio_ring = collections.deque(maxlen=int(30 * AUDIO_RATE / 1200) + 1)  # blocs de 0,2 s
frames = collections.deque(maxlen=int(2.0 * AUDIO_RATE / HOP))          # (pic Hz ou None)
waterfall = collections.deque(maxlen=int(6.0 * AUDIO_RATE / HOP))
events = []
_proc = None
_restart = threading.Event()


def load():
    global events
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            conf.update(json.load(f))
    except (OSError, ValueError):
        pass
    try:
        with open(EVENTS_FILE, encoding="utf-8") as f:
            events = json.load(f)
        for e in events:  # détection interrompue par un arrêt du service
            if e.get("end") is None:
                e["end"] = e["start"]
    except (OSError, ValueError):
        events = []


def save_events():
    try:
        os.makedirs(DATA, exist_ok=True)
        tmp = EVENTS_FILE + ".tmp"
        with _lock:
            data = list(events[:200])
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, EVENTS_FILE)
    except OSError:
        pass


def wav_bytes(samples: np.ndarray) -> bytes:
    pk = float(np.max(np.abs(samples))) if samples.size else 0.0
    gain = 0.8 * 32767 / pk if pk > 0 else 1.0
    pcm = np.clip(samples * gain, -32767, 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def last_audio(seconds: float) -> np.ndarray:
    with _lock:
        blocks = list(audio_ring)
    if not blocks:
        return np.zeros(0, dtype=np.float32)
    a = np.concatenate(blocks)
    return a[-int(seconds * AUDIO_RATE):]


# ------------------------------------------------------------ Analyse --

def frame_peak(seg: np.ndarray):
    """Fréquence de la tonalité dominante (250–1700 Hz) et son « contraste »."""
    spec = np.abs(np.fft.rfft(seg * WIN)) ** 2
    band = spec[9:120]                       # 210 Hz – 2,8 kHz
    med = float(np.median(band)) + 1e-9
    lo, hi = 11, 73                          # 258 – 1710 Hz
    k = lo + int(np.argmax(spec[lo:hi]))
    energy = float(np.sum(spec[max(0, k - 4):k + 5]))
    contrast = energy / (med * 9)
    wf = spec[WF_LO:WF_HI].reshape(-1, WF_GROUP).sum(axis=1)
    return k * BIN_HZ, contrast, wf


def judge(track):
    """Décide si les 2 dernières secondes ressemblent à une sirène de balise."""
    n = len(track)
    if n < 40:
        return 0.0, {}
    freqs = [f for f in track]
    tonal_idx = [i for i, f in enumerate(freqs) if f is not None]
    tonal = len(tonal_idx) / n
    info = {"tonal": tonal, "span_hz": 0, "sweeps_per_s": 0.0, "direction": None}
    if len(tonal_idx) < 15:
        return 0.0, info
    vals = np.array([freqs[i] for i in tonal_idx])
    span = float(np.percentile(vals, 90) - np.percentile(vals, 10))
    small_up = small_down = jumps = 0
    for a, b in zip(tonal_idx, tonal_idx[1:]):
        if b - a > 2:
            continue
        d = freqs[b] - freqs[a]
        if abs(d) >= 350:
            jumps += 1
        elif d > 8:
            small_up += 1
        elif d < -8:
            small_down += 1
    steps = small_up + small_down
    dominance = max(small_up, small_down) / steps if steps else 0.0
    down = small_down >= small_up
    # Balayage régulier : longues suites de trames dont la tonalité glisse
    # dans le même sens (une sirène en fait une par balayage ; la voix non).
    run, in_runs = 0, 0
    for a_, b_ in zip(tonal_idx, tonal_idx[1:]):
        gap = b_ - a_
        step = ((freqs[a_] - freqs[b_]) if down else (freqs[b_] - freqs[a_])) / gap
        if gap <= 2 and 8 < step < 350:
            run += gap
        else:
            if run >= 5:
                in_runs += run
            run = 0
    if run >= 5:
        in_runs += run
    regular = in_runs / n
    dur = n * HOP / AUDIO_RATE
    rate = jumps / dur
    info.update(span_hz=round(span), sweeps_per_s=round(rate, 1), regular=round(regular, 2),
                direction=("descendant" if down else "montant") if steps else None)
    # Tous les critères doivent être réunis (score = le plus faible) :
    # tonalité, balayage d'au moins ~600 Hz, 1 à 7 balayages par seconde,
    # glissements réguliers dans un même sens.
    c_tonal = min(tonal / 0.3, 1.0)
    c_span = min(span / 600, 1.0)
    c_rate = 1.0 if 1.0 <= rate <= 7 else 0.5 if 0.5 <= rate <= 10 else 0.0
    c_dir = min(max(dominance - 0.5, 0) / 0.2, 1.0)
    c_reg = min(regular / 0.25, 1.0)
    score = min(c_tonal, c_span, c_rate, c_dir, c_reg)
    return score, info


class Detector:
    def __init__(self):
        self.pos_count = 0
        self.neg_count = 0
        self.event = None
        self.rec_left = 0
        self.rec_buf = []

    def update(self, score, now):
        positive = score >= 0.99
        if positive:
            self.pos_count += 1
            self.neg_count = 0
        else:
            self.neg_count += 1
            self.pos_count = 0
        if not state["active"] and self.pos_count >= 3:
            self.start(now)
        elif state["active"] and self.neg_count >= 8:
            self.stop(now)
        if self.event is not None and state["above_db"] is not None:
            self.event["max_above_db"] = max(self.event.get("max_above_db") or -99, round(state["above_db"], 1))
            self.event["max_level_db"] = max(self.event.get("max_level_db") or -99, round(state["level_db"], 1))

    def start(self, now):
        state["active"], state["active_since"] = True, now
        name = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + ".wav"
        self.event = {"start": now, "end": None, "freq_mhz": conf["freq_mhz"], "rec": name,
                      "max_above_db": state["above_db"], "max_level_db": state["level_db"],
                      "sweeps_per_s": state["sweeps_per_s"], "direction": state["direction"]}
        with _lock:
            events.insert(0, self.event)
            del events[200:]
        self.rec_buf = [last_audio(4.0)]
        self.rec_left = 8 * AUDIO_RATE
        log.warning("Sirène détectée sur %.3f MHz (%.1f dB au-dessus du bruit)", conf["freq_mhz"],
                    state["above_db"] or 0)
        save_events()

    def stop(self, now):
        state["active"], state["active_since"] = False, None
        if self.event is not None:
            self.event["end"] = now
            self.event = None
            save_events()

    def feed_audio(self, block):
        if self.rec_left > 0:
            self.rec_buf.append(block)
            self.rec_left -= len(block)
            if self.rec_left <= 0:
                self.write_rec()

    def write_rec(self):
        ev = events[0] if events else None
        if not ev or not self.rec_buf:
            return
        try:
            os.makedirs(REC_DIR, exist_ok=True)
            with open(os.path.join(REC_DIR, ev["rec"]), "wb") as f:
                f.write(wav_bytes(np.concatenate(self.rec_buf)))
            files = sorted(os.listdir(REC_DIR))
            for old in files[:-40]:
                os.remove(os.path.join(REC_DIR, old))
        except OSError as exc:
            log.warning("Enregistrement impossible : %s", exc)
        self.rec_buf = []


def run():
    global _proc
    det = Detector()
    while True:
        exe = shutil.which("rtl_fm")
        if not exe:
            state.update(running=False, error="rtl_fm absent (paquet rtl-sdr)")
            time.sleep(30)
            continue
        cmd = [exe, "-d", "0", "-M", "am", "-f", f"{conf['freq_mhz']}M", "-s", str(RF_RATE),
               "-g", str(conf["gain"]), "-p", str(int(conf.get("ppm") or 0)), "-"]
        try:
            _proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            state.update(running=False, error=str(exc))
            time.sleep(20)
            continue
        err = collections.deque(maxlen=8)
        threading.Thread(target=lambda p=_proc: [err.append(l.decode("utf-8", "replace").rstrip())
                                                  for l in p.stderr], daemon=True).start()
        state.update(running=True, error=None, started_at=time.time())
        log.info("Écoute de %.3f MHz (gain %s dB)", conf["freq_mhz"], conf["gain"])
        _restart.clear()
        rest = np.zeros(0, dtype=np.float32)
        sec_acc, sec_n, last_judge = 0.0, 0, 0.0
        chunk_bytes = (RF_RATE // 5) * 2
        pending = b""
        frames.clear()
        while not _restart.is_set():
            data = _proc.stdout.read(chunk_bytes)
            if not data:
                break
            pending += data
            if len(pending) < chunk_bytes:
                continue
            raw = np.frombuffer(pending[:chunk_bytes], dtype="<i2").astype(np.float32)
            pending = pending[chunk_bytes:]
            now = time.time()
            env = float(np.mean(raw))                     # enveloppe = niveau de la porteuse
            sec_acc += env
            sec_n += 1
            audio = (raw - env)[: len(raw) // DEC * DEC].reshape(-1, DEC).mean(axis=1)
            with _lock:
                audio_ring.append(audio)
            det.feed_audio(audio)
            buf = np.concatenate([rest, audio])
            i = 0
            while i + NFFT <= len(buf):
                f, contrast, wf = frame_peak(buf[i:i + NFFT])
                frames.append(f if contrast >= 3.0 else None)
                with _lock:
                    waterfall.append(np.round(10 * np.log10(wf + 1e-3)).astype(int).tolist())
                i += HOP
            rest = buf[i:]
            if sec_n >= 5:                                 # une seconde
                db = 20 * math.log10(max(sec_acc / sec_n, 1e-3))
                sec_acc, sec_n = 0.0, 0
                with _lock:
                    levels.append((round(now, 1), round(db, 1)))
                    if not state["active"]:
                        quiet.append(db)
                    recent = list(quiet)
                floor = float(np.percentile(recent, 20)) if len(recent) >= 5 else db
                state.update(level_db=round(db, 1), floor_db=round(floor, 1), above_db=round(db - floor, 1))
            if now - last_judge >= 0.5:
                last_judge = now
                score, info = judge(list(frames))
                state.update(score=round(score, 2), tonal=round(info.get("tonal", 0), 2),
                             span_hz=info.get("span_hz", 0), sweeps_per_s=info.get("sweeps_per_s", 0.0),
                             direction=info.get("direction"), regular=info.get("regular", 0))
                det.update(score, now)
        try:
            _proc.kill()
        except OSError:
            pass
        code = _proc.wait()
        if state["active"]:
            det.stop(time.time())
        if _restart.is_set():
            continue
        state.update(running=False, error=f"rtl_fm arrêté (code {code})" + (f" : {err[-1]}" if err else ""))
        log.warning("rtl_fm arrêté (code %s) : %s", code, " | ".join(err))
        time.sleep(8)


# -------------------------------------------------------------- HTTP --

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path.rstrip("/"), parse_qs(u.query)
        if path == "/api/elt121/status":
            today = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
            with _lock:
                n_today = sum(1 for e in events if e["start"] >= today)
                last = events[0] if events else None
            self._send(200, {**state, **conf, "events_today": n_today, "last_event": last})
        elif path == "/api/elt121/level":
            secs = float(q.get("seconds", ["900"])[0])
            cut = time.time() - secs
            with _lock:
                self._send(200, [p for p in levels if p[0] >= cut])
        elif path == "/api/elt121/waterfall":
            with _lock:
                rows = list(waterfall)[::2]
            self._send(200, {"f_lo": WF_LO * BIN_HZ, "f_hi": WF_HI * BIN_HZ, "dt": 2 * HOP / AUDIO_RATE,
                             "rows": rows})
        elif path == "/api/elt121/events":
            with _lock:
                self._send(200, events[:200])
        elif path == "/api/elt121/audio":
            secs = min(30.0, float(q.get("seconds", ["10"])[0]))
            self._send(200, wav_bytes(last_audio(secs)), "audio/wav")
        elif path.startswith("/api/elt121/rec/"):
            name = path.rsplit("/", 1)[-1]
            if not re.fullmatch(r"[\d-]+\.wav", name):
                self._send(404, {"error": "introuvable"})
                return
            try:
                with open(os.path.join(REC_DIR, name), "rb") as f:
                    self._send(200, f.read(), "audio/wav")
            except OSError:
                self._send(404, {"error": "enregistrement pas encore prêt ou effacé"})
        else:
            self._send(404, {"error": "introuvable"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/elt121/config":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if "freq_mhz" in body:
                    f = float(body["freq_mhz"])
                    if not 24 <= f <= 1700:
                        raise ValueError("fréquence hors de la plage de la clé")
                    conf["freq_mhz"] = round(f, 4)
                if "gain" in body:
                    conf["gain"] = max(0.0, min(49.6, float(body["gain"])))
                if "ppm" in body:
                    conf["ppm"] = int(body["ppm"])
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": str(exc)})
                return
            try:
                os.makedirs(DATA, exist_ok=True)
                with open(CONF_FILE, "w", encoding="utf-8") as f:
                    json.dump(conf, f)
            except OSError:
                pass
            with _lock:
                levels.clear()
                quiet.clear()
            _restart.set()
            self._send(200, {"ok": True, **conf})
        elif path == "/api/elt121/clear":
            with _lock:
                events.clear()
            save_events()
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "introuvable"})

    def log_message(self, *_a):
        pass


if __name__ == "__main__":
    load()
    threading.Thread(target=run, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
