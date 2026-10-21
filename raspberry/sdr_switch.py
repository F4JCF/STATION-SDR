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
    # Décodage des balises de détresse 406 MHz (écoute seule).
    "beacon": ["beacon406"],
}

LABELS = {"adsb": "ADS-B", "ais": "AIS", "aprs": "APRS", "openwebrx": "OpenWebRX", "beacon": "Balise 406",
          "off": "Arrêtée"}
# "off" : aucun module ne tient la clé (tout est arrêté) ; le partage
# automatique est suspendu jusqu'à ce qu'on redonne la clé à un module.
OFF = "off"

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
        if mode in CANDIDATES or mode == OFF:
            return mode
    except OSError:
        pass
    return DEFAULT_MODE


LAST_MODE_FILE = os.path.join(STATE_DIR, "last_mode")


def _read_last_mode():
    try:
        with open(LAST_MODE_FILE, "r", encoding="utf-8") as f:
            mode = f.read().strip()
        return mode if mode in CANDIDATES else None
    except OSError:
        return None


def _write_mode(mode: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    if mode != OFF:
        with open(LAST_MODE_FILE, "w", encoding="utf-8") as f:
            f.write(mode)
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
            "ok": (bool(started) and not failed) or (mode == OFF),
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
            if _read_mode() == OFF:
                wanted = None  # clé arrêtée volontairement : le planning attend
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


# --------------------------------------------------------- Maintenance --

# Services dont on peut lire le journal depuis l'appli (liste fermée :
# l'appli ne peut pas demander n'importe quelle commande au Pi).
LOG_UNITS = {
    "readsb": "ADS-B (readsb)", "direwolf": "APRS (Direwolf)", "ais-catcher": "AIS (AIS-catcher)",
    "svxlink": "SvxLink", "aprs-api": "API APRS", "svxlink-api": "API SvxLink", "sdr-switch": "Bascule de clé",
    "station-monitor": "Surveillance / notifications", "pat@f4jcf": "Winlink (Pat)", "docker:openwebrx": "OpenWebRX",
    "beacon406": "Balises 406 MHz",
}

# Fichiers de configuration inclus dans la sauvegarde.
BACKUP_PATHS = [
    "/etc/direwolf.conf", "/etc/systemd/system/direwolf.service.d",
    "/etc/svxlink", "/etc/default/svxlink",
    "/etc/AIS-catcher",
    "/etc/default/readsb",
    "/var/lib/sdr-switch", "/etc/station-monitor.json",
    "/home/f4jcf/.config/pat/config.json",
    "/etc/systemd/system/sdr-switch.service", "/etc/systemd/system/aprs-api.service",
    "/etc/systemd/system/svxlink-api.service", "/etc/systemd/system/station-monitor.service",
    "/var/lib/alsa/asound.state", "/etc/voicecard",
]


def _service_logs(unit: str, lines: int = 60) -> str:
    lines = max(10, min(300, lines))
    try:
        if unit.startswith("docker:"):
            res = _docker("logs", "--tail", str(lines), unit[7:])
            return (res.stdout + res.stderr)[-60000:]
        if unit == "svxlink" and os.path.exists("/var/log/svxlink"):
            res = subprocess.run(["tail", "-n", str(lines), "/var/log/svxlink"], capture_output=True, text=True, timeout=10)
            return res.stdout[-60000:]
        res = subprocess.run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"],
                             capture_output=True, text=True, timeout=15)
        return (res.stdout or res.stderr)[-60000:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Lecture impossible : {exc}"


def _backup_tar() -> bytes:
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in BACKUP_PATHS:
            if os.path.exists(path):
                try:
                    tar.add(path, arcname=path.lstrip("/"))
                except OSError:
                    pass
        info = tarfile.TarInfo("LISEZMOI.txt")
        txt = ("Sauvegarde des configurations de F4JCF-SDR-STATION ("
               + time.strftime("%d/%m/%Y %H:%M") + ").\nRestauration : copier chaque fichier "
               "à son emplacement d'origine (chemins conservés dans l'archive), puis redémarrer le Pi.\n"
               "Attention : contient des mots de passe (Winlink, EchoLink) : à garder en lieu sûr.\n").encode("utf-8")
        info.size = len(txt)
        info.mtime = time.time()
        tar.addfile(info, io.BytesIO(txt))
    return buf.getvalue()


# --------------------------------------------------- Mise à jour du Pi --
#
# L'appli demande d'installer les scripts de SA version (tag vX.Y.Z du
# dépôt public). Liste de fichiers fermée : rien d'autre ne peut être
# téléchargé ou écrit par ce biais.

REPO_RAW = "https://raw.githubusercontent.com/F4JCF/STATION-SDR"
PI_FILES = [
    # (fichier du dépôt, destination sur le Pi, mode)
    ("raspberry/sdr_switch.py", "/usr/local/bin/sdr_switch.py", 0o755),
    ("raspberry/station_monitor.py", "/usr/local/bin/station_monitor.py", 0o755),
    ("raspberry/svxlink_api.py", "/usr/local/bin/svxlink_api.py", 0o755),
    ("raspberry/aprs_api.py", "/home/f4jcf/aprs_api.py", 0o755),
    ("raspberry/sdr-switch.service", "/etc/systemd/system/sdr-switch.service", 0o644),
    ("raspberry/station-monitor.service", "/etc/systemd/system/station-monitor.service", 0o644),
    ("raspberry/svxlink-api.service", "/etc/systemd/system/svxlink-api.service", 0o644),
    ("raspberry/aprs-api.service", "/etc/systemd/system/aprs-api.service", 0o644),
    ("raspberry/direwolf-sdr.conf", "/etc/systemd/system/direwolf.service.d/sdr.conf", 0o644),
    ("raspberry/beacon406_api.py", "/usr/local/bin/beacon406_api.py", 0o755),
    ("raspberry/beacon406.service", "/etc/systemd/system/beacon406.service", 0o644),
]
PI_VERSION_FILE = os.path.join(STATE_DIR, "pi_version")
_update_state = {"running": False, "log": [], "result": None}


def _read_pi_version():
    try:
        with open(PI_VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _update_pi(ref: str) -> None:
    import urllib.request
    log_lines = _update_state["log"]
    log_lines.clear()

    def say(msg):
        log.info("Mise à jour : %s", msg)
        log_lines.append(msg)

    try:
        say(f"Téléchargement des scripts de la version {ref}")
        fetched = []
        for src, dest, mode in PI_FILES:
            if not os.path.exists(dest) and "sdr_switch" not in dest and "sdr-switch" not in dest:
                continue  # composant jamais installé sur ce Pi : on ne l'ajoute pas
            url = f"{REPO_RAW}/{ref}/{src}"
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read()
            if not data:
                raise RuntimeError(f"fichier vide : {src}")
            if src.endswith(".py"):
                compile(data, src, "exec")  # refuse un script illisible
            fetched.append((data, dest, mode))
            say(f"  {src} téléchargé ({len(data)} octets)")
        for data, dest, mode in fetched:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".new"
            with open(tmp, "wb") as f:
                f.write(data)
            os.chmod(tmp, mode)
            if dest.startswith("/home/f4jcf/"):
                try:
                    import pwd
                    pw = pwd.getpwnam("f4jcf")
                    os.chown(tmp, pw.pw_uid, pw.pw_gid)
                except (KeyError, OSError):
                    pass
            os.replace(tmp, dest)
        say(f"{len(fetched)} fichier(s) installé(s)")
        _systemctl("daemon-reload")
        for unit in ("aprs-api", "svxlink-api", "station-monitor"):
            if _systemctl("is-enabled", f"{unit}.service").stdout.strip() == "enabled":
                _systemctl("restart", f"{unit}.service")
                say(f"  {unit} redémarré")
        if _systemctl("is-active", "beacon406.service").stdout.strip() == "active":
            _systemctl("restart", "beacon406.service")
            say("  beacon406 redémarré")
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(PI_VERSION_FILE, "w", encoding="utf-8") as f:
            f.write(ref)
        _update_state["result"] = {"ok": True, "version": ref, "time": time.time()}
        say("Terminé. Le service de bascule redémarre (quelques secondes).")
        time.sleep(1)
        subprocess.run(["systemctl", "--no-block", "restart", "sdr-switch.service"], timeout=10)
    except Exception as exc:
        say(f"ÉCHEC : {exc}")
        _update_state["result"] = {"ok": False, "error": str(exc), "time": time.time()}
    finally:
        _update_state["running"] = False


def _power_action(action: str) -> None:
    time.sleep(2)  # laisse le temps à la réponse HTTP de partir
    subprocess.run(["systemctl", "reboot" if action == "reboot" else "poweroff"], timeout=30)


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
        "last_mode": _read_last_mode(),
        "pi_version": _read_pi_version(),
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
        elif path == "/api/system/logs":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            unit = (q.get("unit") or [""])[0]
            if unit not in LOG_UNITS:
                self._send(400, {"error": "service non autorisé", "units": LOG_UNITS})
                return
            try:
                lines = int((q.get("lines") or ["60"])[0])
            except ValueError:
                lines = 60
            self._send(200, {"unit": unit, "label": LOG_UNITS[unit], "text": _service_logs(unit, lines)})
        elif path == "/api/system/update":
            self._send(200, {**_update_state, "pi_version": _read_pi_version()})
        elif path == "/api/system/log-units":
            self._send(200, LOG_UNITS)
        elif path == "/api/system/backup":
            data = _backup_tar()
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition",
                             f'attachment; filename="station-sdr-config-{time.strftime("%Y%m%d-%H%M")}.tar.gz"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send(404, {"error": "introuvable"})

    def do_POST(self):
        path = self._path()
        if path in ("/api/system/reboot", "/api/system/shutdown"):
            action = "reboot" if path.endswith("reboot") else "shutdown"
            log.warning("Demande de %s depuis %s", action, self.address_string())
            self._send(200, {"ok": True, "action": action})
            threading.Thread(target=_power_action, args=(action,), daemon=True).start()
            return
        if path == "/api/system/update":
            from urllib.parse import parse_qs, urlparse
            import re as _re
            ref = (parse_qs(urlparse(self.path).query).get("ref") or [""])[0]
            if not _re.fullmatch(r"v\d+\.\d+\.\d+", ref):
                self._send(400, {"error": "version invalide (attendu vX.Y.Z)"})
                return
            if _update_state["running"]:
                self._send(409, {"error": "mise à jour déjà en cours"})
                return
            _update_state.update(running=True, result=None)
            threading.Thread(target=_update_pi, args=(ref,), daemon=True).start()
            self._send(200, {"started": True, "ref": ref})
            return
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
        if mode == OFF:
            self._send(200, switch_to(OFF))
            return
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
        if boot_mode == OFF or UNITS.get(boot_mode):
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
