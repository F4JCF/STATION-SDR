#!/usr/bin/env python3
"""
Bascule de l'unique clé SDR entre les modules (F4JCF-SDR-STATION)

Une seule clé RTL-SDR ne peut servir qu'un programme à la fois. Ce petit
service arrête les programmes des autres modules puis démarre celui du
module demandé, pour que la clé passe proprement de l'un à l'autre.

Endpoints :
  GET  /api/sdr/mode          -> mode actuel + services détectés par mode
  POST /api/sdr/mode/<mode>   -> bascule la clé vers ce mode
                                 (adsb, ais, aprs, openwebrx)

Le dernier mode choisi est mémorisé et réappliqué au démarrage du Pi, ce
qui évite que plusieurs programmes se disputent la clé au boot.

Doit tourner en root (il pilote systemctl). Port : SDR_SWITCH_PORT (5006).
"""

import glob
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Bibliothèque standard uniquement : ce service tourne en root, et Flask
# n'est souvent installé que pour l'utilisateur courant (pip --user).

PORT = int(os.environ.get("SDR_SWITCH_PORT", "5006"))
STATE_DIR = "/var/lib/sdr-switch"
STATE_FILE = os.path.join(STATE_DIR, "mode")
DEFAULT_MODE = "adsb"

# Noms de services possibles pour chaque module ; seuls ceux qui existent
# réellement sur le Pi sont utilisés.
CANDIDATES = {
    "adsb": ["readsb", "dump1090-fa", "dump1090"],
    "ais": ["ais-catcher", "aiscatcher", "AIS-catcher", "rtl-ais", "rtl_ais"],
    "aprs": ["direwolf"],
    # OpenWebRX tourne dans un conteneur Docker sur ce Pi.
    "openwebrx": ["openwebrx", "docker:openwebrx"],
}

LABELS = {"adsb": "ADS-B", "ais": "AIS", "aprs": "APRS", "openwebrx": "OpenWebRX"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sdr-switch")
_lock = threading.Lock()


def _systemctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=60)


def _docker(*args) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def _unit_exists(name: str) -> bool:
    if name.startswith("docker:"):
        return _docker("inspect", name[7:]).returncode == 0
    res = _systemctl("list-unit-files", f"{name}.service", "--no-legend")
    return res.returncode == 0 and f"{name}.service" in res.stdout


def _detect_units() -> dict:
    return {mode: [n for n in names if _unit_exists(n)] for mode, names in CANDIDATES.items()}


UNITS = _detect_units()


def _read_mode() -> str:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            mode = f.read().strip()
        if mode in CANDIDATES:
            return mode
    except OSError:
        pass
    return DEFAULT_MODE


def _write_mode(mode: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(mode)


def _is_active(unit: str) -> bool:
    if unit.startswith("docker:"):
        res = _docker("inspect", "-f", "{{.State.Running}}", unit[7:])
        return res.stdout.strip() == "true"
    return _systemctl("is-active", f"{unit}.service").stdout.strip() == "active"


def _stop(unit: str) -> None:
    if unit.startswith("docker:"):
        _docker("stop", unit[7:])
    else:
        _systemctl("stop", f"{unit}.service")


def _start(unit: str) -> bool:
    if unit.startswith("docker:"):
        return _docker("restart", unit[7:]).returncode == 0
    return _systemctl("restart", f"{unit}.service").returncode == 0


def switch_to(mode: str) -> dict:
    with _lock:
        stopped = []
        for other, units in UNITS.items():
            if other == mode:
                continue
            for unit in units:
                if _is_active(unit):
                    _stop(unit)
                    stopped.append(unit)
        if stopped:
            # Laisse le temps au pilote USB de libérer la clé.
            time.sleep(2)

        started, failed = [], []
        for unit in UNITS.get(mode, []):
            (started if _start(unit) else failed).append(unit)

        _write_mode(mode)
        log.info("Mode %s : arrêtés=%s démarrés=%s échecs=%s", mode, stopped, started, failed)
        return {
            "mode": mode,
            "label": LABELS.get(mode, mode),
            "stopped": stopped,
            "started": started,
            "failed": failed,
            "ok": bool(started) and not failed,
        }


# ------------------------------------------------------------- Planning --
#
# Trois façons de choisir le module qui détient la clé :
#   "manual"   : la clé reste où on l'a mise (défaut)
#   "rotation" : cycle [[mode, minutes], ...] répété en boucle
#   "planning" : créneaux horaires [{"start": "HH:MM", "mode": ...}, ...]
# Une bascule manuelle (ouverture d'une page dans l'appli) suspend le
# planning pendant `hold_minutes`, puis il reprend tout seul.

SCHEDULE_FILE = os.path.join(STATE_DIR, "schedule.json")
DEFAULT_SCHEDULE = {
    "type": "manual",
    "rotation": [["adsb", 20], ["ais", 10], ["aprs", 10]],
    "planning": [{"start": "07:00", "mode": "aprs"}, {"start": "22:00", "mode": "adsb"}],
    "hold_minutes": 15,
}
_sched_state = {"hold_until": 0.0, "rotation_index": 0, "rotation_started": 0.0, "next_change": None}


def _read_schedule() -> dict:
    sched = json.loads(json.dumps(DEFAULT_SCHEDULE))
    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            sched.update(json.load(f))
    except (OSError, ValueError):
        pass
    return sched


def _validate_schedule(data: dict) -> dict:
    sched = _read_schedule()
    if data.get("type") in ("manual", "rotation", "planning"):
        sched["type"] = data["type"]
    if isinstance(data.get("rotation"), list):
        rot = []
        for item in data["rotation"]:
            try:
                mode, minutes = item[0], int(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if mode in CANDIDATES and 1 <= minutes <= 24 * 60:
                rot.append([mode, minutes])
        if rot:
            sched["rotation"] = rot
    if isinstance(data.get("planning"), list):
        plan = []
        for item in data["planning"]:
            try:
                hh, mm = (int(x) for x in str(item.get("start", "")).split(":"))
            except (ValueError, AttributeError):
                continue
            if 0 <= hh < 24 and 0 <= mm < 60 and item.get("mode") in CANDIDATES:
                plan.append({"start": f"{hh:02d}:{mm:02d}", "mode": item["mode"]})
        if plan:
            sched["planning"] = sorted(plan, key=lambda x: x["start"])
    try:
        sched["hold_minutes"] = max(0, min(240, int(data.get("hold_minutes", sched["hold_minutes"]))))
    except (TypeError, ValueError):
        pass
    return sched


def _write_schedule(sched: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(sched, f, ensure_ascii=False, indent=2)
    _sched_state.update(rotation_index=0, rotation_started=time.time(), hold_until=0.0)


def _planning_mode(plan: list, now: float):
    """Mode du créneau en cours et heure du prochain changement."""
    lt = time.localtime(now)
    minutes_now = lt.tm_hour * 60 + lt.tm_min
    slots = [(int(p["start"][:2]) * 60 + int(p["start"][3:]), p["mode"]) for p in plan]
    if not slots:
        return None, None
    current = slots[-1][1]
    for start, mode in slots:
        if start <= minutes_now:
            current = mode
    nxt = next((st for st, _m in slots if st > minutes_now), slots[0][0] + 24 * 60)
    midnight = now - (minutes_now * 60 + lt.tm_sec)
    return current, midnight + nxt * 60


def _scheduler_loop() -> None:
    while True:
        try:
            sched = _read_schedule()
            now = time.time()
            wanted = None
            if sched["type"] == "rotation" and sched["rotation"]:
                rot = sched["rotation"]
                idx = _sched_state["rotation_index"] % len(rot)
                if not _sched_state["rotation_started"]:
                    _sched_state["rotation_started"] = now
                end = _sched_state["rotation_started"] + rot[idx][1] * 60
                if now >= end and now >= _sched_state["hold_until"]:
                    idx = (idx + 1) % len(rot)
                    _sched_state.update(rotation_index=idx, rotation_started=now)
                    end = now + rot[idx][1] * 60
                wanted = rot[idx][0]
                _sched_state["next_change"] = end
            elif sched["type"] == "planning":
                wanted, _sched_state["next_change"] = _planning_mode(sched["planning"], now)
            else:
                _sched_state["next_change"] = None
            if wanted and now >= _sched_state["hold_until"] and wanted != _read_mode() and UNITS.get(wanted):
                log.info("Planning (%s) : bascule vers %s", sched["type"], wanted)
                switch_to(wanted)
        except Exception:
            log.exception("Erreur du planning")
        time.sleep(20)


# --------------------------------------------------------------- Santé --

THROTTLE_BITS = {
    0: "under_voltage_now", 1: "freq_capped_now", 2: "throttled_now", 3: "temp_limit_now",
    16: "under_voltage_occurred", 17: "freq_capped_occurred", 18: "throttled_occurred",
    19: "temp_limit_occurred",
}


def _read(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip().strip("\x00")
    except OSError:
        return None


def _health() -> dict:
    out = {"time": time.time()}
    t = _read("/sys/class/thermal/thermal_zone0/temp")
    out["cpu_temp_c"] = round(int(t) / 1000, 1) if t and t.isdigit() else None
    out["model"] = _read("/proc/device-tree/model")
    out["hostname"] = os.uname().nodename
    out["kernel"] = os.uname().release

    raw = None
    try:
        res = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5)
        if "=" in res.stdout:
            raw = int(res.stdout.strip().split("=")[1], 16)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    out["throttled_raw"] = None if raw is None else hex(raw)
    out["throttled"] = {name: bool(raw & (1 << bit)) for bit, name in THROTTLE_BITS.items()} if raw is not None else None

    try:
        load1, load5, load15 = os.getloadavg()
        out["load"] = [round(load1, 2), round(load5, 2), round(load15, 2)]
    except OSError:
        out["load"] = None
    out["cpu_count"] = os.cpu_count()

    mem = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = int(v.split()[0]) * 1024
    out["mem_total"], out["mem_available"] = mem.get("MemTotal"), mem.get("MemAvailable")

    try:
        du = shutil.disk_usage("/")
        out["disk_total"], out["disk_free"] = du.total, du.free
    except OSError:
        out["disk_total"] = out["disk_free"] = None

    up = _read("/proc/uptime")
    out["uptime_s"] = int(float(up.split()[0])) if up else None

    # Clés RTL-SDR présentes sur l'USB (Realtek 0bda:2832 / 0bda:2838).
    count = 0
    for dev in glob.glob("/sys/bus/usb/devices/*"):
        if _read(os.path.join(dev, "idVendor")) == "0bda" and _read(os.path.join(dev, "idProduct")) in ("2832", "2838"):
            count += 1
    out["sdr_dongles"] = count
    return out


# --------------------------------------------------------------- Serveur --

def _mode_status() -> dict:
    mode = _read_mode()
    sched = _read_schedule()
    return {
        "mode": mode,
        "label": LABELS.get(mode, mode),
        "units": UNITS,
        "active": {m: any(_is_active(u) for u in us) for m, us in UNITS.items()},
        "schedule": sched,
        "hold_until": _sched_state["hold_until"] or None,
        "next_change": _sched_state["next_change"],
        "labels": LABELS,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path(self):
        return self.path.split("?", 1)[0].rstrip("/")

    def do_GET(self):
        path = self._path()
        if path == "/api/sdr/mode":
            self._send(200, _mode_status())
        elif path == "/api/system/health":
            self._send(200, _health())
        else:
            self._send(404, {"error": "introuvable"})

    def do_POST(self):
        path = self._path()
        if path == "/api/sdr/schedule":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, OSError):
                self._send(400, {"error": "JSON invalide"})
                return
            sched = _validate_schedule(data if isinstance(data, dict) else {})
            _write_schedule(sched)
            self._send(200, _mode_status())
            return
        prefix = "/api/sdr/mode/"
        if not path.startswith(prefix):
            self._send(404, {"error": "introuvable"})
            return
        mode = path[len(prefix):].strip("/")
        if mode not in CANDIDATES:
            self._send(400, {"error": f"mode inconnu : {mode}"})
        elif not UNITS.get(mode):
            self._send(404, {"error": f"aucun service installé pour {LABELS.get(mode, mode)}"})
        else:
            sched = _read_schedule()
            if sched["type"] != "manual":
                # Bascule manuelle : le planning attend avant de reprendre la main.
                _sched_state["hold_until"] = time.time() + sched["hold_minutes"] * 60
                if sched["type"] == "rotation":
                    for i, (m, _mins) in enumerate(sched["rotation"]):
                        if m == mode:
                            _sched_state.update(rotation_index=i, rotation_started=time.time())
                            break
            result = switch_to(mode)
            result["hold_until"] = _sched_state["hold_until"] or None
            self._send(200, result)

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)


def _apply_boot_mode():
    try:
        boot_mode = _read_mode()
        if UNITS.get(boot_mode):
            switch_to(boot_mode)
    except Exception:
        log.exception("Échec de la bascule au démarrage")


if __name__ == "__main__":
    log.info("Services détectés : %s", UNITS)
    # Le serveur répond tout de suite ; la bascule de démarrage se fait
    # en parallèle (elle peut prendre plusieurs secondes).
    threading.Thread(target=_apply_boot_mode, daemon=True).start()
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
