"""Appels aux APIs exposées par le Raspberry Pi F4JCF-SDR-STATION.

Chaque fonction renvoie soit les données JSON décodées, soit lève une
exception (requests.RequestException ou ValueError) que l'appelant
(l'interface graphique) attrape pour afficher un statut "indisponible"
au lieu de planter.
"""

import requests

TIMEOUT = 4  # secondes


def get_weather(host: str, port: int) -> dict:
    """Interroge /api/meteo sur meteo_local.py."""
    url = f"http://{host}:{port}/api/meteo"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_aircraft(host: str, port: int) -> dict:
    """Interroge /data/aircraft.json sur le serveur readsb/tar1090."""
    url = f"http://{host}:{port}/data/aircraft.json"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_services_status(host: str, port: int) -> dict:
    """Interroge /api/services/status sur station_api.py."""
    url = f"http://{host}:{port}/api/services/status"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_sdr_status(host: str, port: int) -> dict:
    """Interroge /api/sdr/status sur sdr_manager.py."""
    url = f"http://{host}:{port}/api/sdr/status"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_aprs_stations(host: str, port: int) -> list:
    """Interroge /api/aprs/stations sur aprs_api.py (décodage Direwolf)."""
    url = f"http://{host}:{port}/api/aprs/stations"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_svxlink_status(host: str, port: int) -> dict:
    """Activité en direct de SvxLink (svxlink_api.py, /api/svxlink/status)."""
    url = f"http://{host}:{port}/api/svxlink/status"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_ais_ships(host: str, port: int) -> dict:
    """Navires suivis par AIS-catcher (serveur web intégré, /api/ships.json).
    Réponse : {"count": N, "ships": [{"mmsi", "shipname", "lat", "lon",
    "speed", "cog", "distance", "last_signal", ...}]}."""
    url = f"http://{host}:{port}/api/ships.json"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def set_sdr_mode(host: str, port: int, mode: str) -> dict:
    """Bascule l'unique clé SDR vers un module via sdr_switch.py (POST).
    Délai plus long : arrêter un service puis en démarrer un autre prend
    quelques secondes."""
    url = f"http://{host}:{port}/api/sdr/mode/{mode}"
    resp = requests.post(url, timeout=45)
    resp.raise_for_status()
    return resp.json()


def set_sdr_schedule(host: str, port: int, schedule: dict) -> dict:
    """Enregistre le partage automatique de la clé (sdr_switch.py)."""
    url = f"http://{host}:{port}/api/sdr/schedule"
    resp = requests.post(url, json=schedule, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_system_health(host: str, port: int) -> dict:
    """Santé du Pi (température, sous-tension, ressources) via sdr_switch.py."""
    url = f"http://{host}:{port}/api/system/health"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def pi_power_action(host: str, port: int, action: str) -> dict:
    """Redémarre ("reboot") ou éteint ("shutdown") le Pi (sdr_switch.py)."""
    resp = requests.post(f"http://{host}:{port}/api/system/{action}", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_pi_backup(host: str, port: int) -> bytes:
    """Archive .tar.gz des configurations du Pi."""
    resp = requests.get(f"http://{host}:{port}/api/system/backup", timeout=60)
    resp.raise_for_status()
    return resp.content


def get_service_log(host: str, port: int, unit: str, lines: int = 60) -> dict:
    resp = requests.get(f"http://{host}:{port}/api/system/logs", params={"unit": unit, "lines": lines}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_stats_series(host: str, port: int, hours: float) -> dict:
    resp = requests.get(f"http://{host}:{port}/api/stats/series", params={"hours": hours}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_stats_polar(host: str, port: int, kind: str, hours: float) -> dict:
    resp = requests.get(f"http://{host}:{port}/api/stats/polar", params={"kind": kind, "hours": hours}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_vigilance(host: str, port: int) -> dict:
    """Vigilance Météo-France du département (relevée par station_monitor.py)."""
    resp = requests.get(f"http://{host}:{port}/api/vigilance", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_monitor_config(host: str, port: int) -> dict:
    resp = requests.get(f"http://{host}:{port}/api/monitor/config", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def set_monitor_config(host: str, port: int, cfg: dict) -> dict:
    resp = requests.post(f"http://{host}:{port}/api/monitor/config", json=cfg, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def monitor_test(host: str, port: int) -> dict:
    resp = requests.post(f"http://{host}:{port}/api/monitor/test", timeout=40)
    resp.raise_for_status()
    return resp.json()


def pi_update(host: str, port: int, ref: str) -> dict:
    """Demande au Pi d'installer les scripts de la version `ref` (vX.Y.Z)."""
    resp = requests.post(f"http://{host}:{port}/api/system/update", params={"ref": ref}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_pi_update_status(host: str, port: int) -> dict:
    resp = requests.get(f"http://{host}:{port}/api/system/update", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_sdr_mode(host: str, port: int) -> dict:
    """Mode actuel de la clé SDR (sdr_switch.py)."""
    url = f"http://{host}:{port}/api/sdr/mode"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_aprs_packets(host: str, port: int) -> list:
    """Derniers paquets APRS bruts (aprs_api.py) : sert à repérer les
    messages adressés à la station."""
    url = f"http://{host}:{port}/api/aprs/packets"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_pat_status(host: str, port: int) -> dict:
    """État de Pat (client Winlink) : connexion en cours, écoutes actives."""
    resp = requests.get(f"http://{host}:{port}/api/status", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_pat_mailbox(host: str, port: int, box: str) -> list:
    """Messages d'une boîte Pat : in, out, sent, archive."""
    resp = requests.get(f"http://{host}:{port}/api/mailbox/{box}", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json() or []


def get_pat_message(host: str, port: int, box: str, mid: str) -> dict:
    resp = requests.get(f"http://{host}:{port}/api/mailbox/{box}/{mid}", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def pat_mark_read(host: str, port: int, box: str, mid: str) -> None:
    requests.post(f"http://{host}:{port}/api/mailbox/{box}/{mid}/read", timeout=TIMEOUT).raise_for_status()


def pat_connect(host: str, port: int, alias: str) -> dict:
    """Lance un relevé Winlink (ex. alias "telnet" = CMS par Internet).
    Peut durer plusieurs minutes selon le volume de courrier."""
    resp = requests.post(f"http://{host}:{port}/api/connect", params={"url": alias}, timeout=600)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return {}


def restart_service(host: str, port: int, key: str) -> dict:
    """Demande le redémarrage d'un service via station_api.py (POST)."""
    url = f"http://{host}:{port}/api/services/{key}/restart"
    resp = requests.post(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()
