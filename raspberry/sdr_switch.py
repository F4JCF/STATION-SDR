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

import logging
import os
import subprocess
import threading
import time

from flask import Flask, jsonify

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
app = Flask(__name__)
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


@app.get("/api/sdr/mode")
def get_mode():
    mode = _read_mode()
    return jsonify({
        "mode": mode,
        "label": LABELS.get(mode, mode),
        "units": UNITS,
        "active": {m: any(_is_active(u) for u in us) for m, us in UNITS.items()},
    })


@app.post("/api/sdr/mode/<mode>")
def set_mode(mode):
    if mode not in CANDIDATES:
        return jsonify({"error": f"mode inconnu : {mode}"}), 400
    if not UNITS.get(mode):
        return jsonify({"error": f"aucun service installé pour {LABELS.get(mode, mode)}"}), 404
    return jsonify(switch_to(mode))


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
    app.run(host="0.0.0.0", port=PORT, threaded=True)
