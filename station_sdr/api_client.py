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


def restart_service(host: str, port: int, key: str) -> dict:
    """Demande le redémarrage d'un service via station_api.py (POST)."""
    url = f"http://{host}:{port}/api/services/{key}/restart"
    resp = requests.post(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()
