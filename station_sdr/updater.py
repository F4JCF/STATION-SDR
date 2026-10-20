"""Mise à jour automatique de STATION-SDR.exe depuis les releases GitHub.

Fonctionnement :
1. Au démarrage, on interroge l'API GitHub pour la dernière release du
   dépôt F4JCF/STATION-SDR.
2. Si son tag (ex: v1.2.0) est plus récent que la version embarquée
   dans l'exécutable courant, on propose la mise à jour à l'utilisateur.
3. S'il accepte, on télécharge le nouvel .exe depuis la release, puis on
   lance un petit script batch qui attend la fermeture de l'application,
   remplace l'ancien .exe par le nouveau, et relance le programme.

Le dépôt étant privé, un jeton GitHub (lecture seule) est nécessaire
pour que les appels API et le téléchargement de l'asset fonctionnent.
Ce jeton n'est jamais embarqué dans l'exécutable : il est saisi une
fois par l'utilisateur dans les Paramètres et stocké uniquement dans
son fichier de configuration local.
"""

import os
import subprocess
import sys
import tempfile

import requests

from .version import __version__

REPO = "F4JCF/STATION-SDR"
API_LATEST_RELEASE = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT = 6


def _parse_version(tag: str) -> tuple:
    """'v1.2.0' -> (1, 2, 0). Retombe sur (0,) si format inattendu."""
    cleaned = tag.lstrip("vV")
    parts = []
    for p in cleaned.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts) if parts else (0,)


def check_for_update(github_token: str = "") -> dict | None:
    """Retourne les infos de la release si une mise à jour est disponible,
    sinon None. Lève requests.RequestException en cas d'échec réseau/API
    (laissé à l'appelant de décider s'il affiche une erreur ou se tait)."""
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    resp = requests.get(API_LATEST_RELEASE, headers=headers, timeout=TIMEOUT)
    if resp.status_code == 404:
        # Dépôt privé sans jeton valide, ou pas encore de release publiée
        return None
    resp.raise_for_status()
    data = resp.json()

    latest_tag = data.get("tag_name", "")
    if _parse_version(latest_tag) <= _parse_version(f"v{__version__}"):
        return None

    exe_asset = next(
        (a for a in data.get("assets", []) if a["name"].lower().endswith(".exe")),
        None,
    )
    if not exe_asset:
        return None

    return {
        "tag": latest_tag,
        "notes": data.get("body", ""),
        "asset_url": exe_asset["url"],  # URL API, pas browser_download_url (dépôt privé)
        "asset_name": exe_asset["name"],
    }


def download_asset(asset_api_url: str, github_token: str, dest_path: str) -> None:
    """Télécharge un asset de release privée via l'API GitHub.

    L'URL browser_download_url classique ne fonctionne pas sans session
    web pour un dépôt privé : il faut passer par l'API avec
    Accept: application/octet-stream.
    """
    headers = {"Accept": "application/octet-stream"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    with requests.get(asset_api_url, headers=headers, timeout=30, stream=True) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)


def apply_update_and_restart(new_exe_path: str) -> None:
    """Remplace l'exécutable courant par le nouveau et relance l'application.

    On ne peut pas remplacer un .exe pendant qu'il tourne sous Windows,
    donc on délègue le remplacement à un script batch lancé en détaché,
    qui attend que ce process se termine avant d'agir.
    """
    current_exe = sys.executable
    pid = os.getpid()

    batch_content = f"""@echo off
:wait
tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL
if not errorlevel 1 (
    timeout /t 1 /nobreak >NUL
    goto wait
)
move /Y "{new_exe_path}" "{current_exe}" >NUL
start "" "{current_exe}"
del "%~f0"
"""
    batch_path = os.path.join(tempfile.gettempdir(), "station_sdr_update.bat")
    with open(batch_path, "w", encoding="ascii") as f:
        f.write(batch_content)

    subprocess.Popen(
        ["cmd", "/c", batch_path],
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    )
    sys.exit(0)
