"""Gestion de la configuration locale de STATION-SDR.

La configuration est stockée dans un fichier JSON sous le dossier de
données de l'utilisateur Windows (%APPDATA%\\STATION-SDR\\config.json),
donc elle persiste entre les mises à jour de l'exécutable et n'a pas
besoin d'être réécrite dans le dossier d'installation (souvent en
lecture seule ou effacé lors d'une mise à jour).
"""

import json
from pathlib import Path

APP_NAME = "STATION-SDR"

DEFAULT_CONFIG = {
    "pi_host": "192.168.1.229",
    "port_weather": 5001,
    "port_adsb": 8504,
    "port_services": 5003,
    "port_sdr": 5002,
    "port_aprs": 5004,
    "port_sdr_switch": 5006,
    "port_ais": 8100,
    "port_svxlink": 5007,
    "port_winlink": 8080,
    "port_monitor": 5008,
    "port_beacon": 5009,
    "lora_igate_host": "192.168.1.154",
    "port_433": 5010,
    "port_elt121": 5011,
    "port_openwebrx": 8073,
    "refresh_seconds": 15,
    "check_updates": True,
    # Jeton GitHub personnel (optionnel). Nécessaire uniquement si le
    # dépôt F4JCF/STATION-SDR reste privé et que la mise à jour
    # automatique doit fonctionner. Laisser vide désactive seulement
    # la vérification de mise à jour, le reste de l'application
    # fonctionne normalement.
    "github_token": "",
}


def _config_dir() -> Path:
    # Windows : %APPDATA%\\STATION-SDR ; Linux : ~/.config/STATION-SDR
    from .plat import config_dir
    return config_dir()


def _config_path() -> Path:
    return _config_dir() / "config.json"


def load_config() -> dict:
    path = _config_path()
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            config.update(saved)
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save_config(config: dict) -> None:
    config_dir = _config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    path = _config_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
