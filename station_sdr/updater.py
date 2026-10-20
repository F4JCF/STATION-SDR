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


class PrivateRepoAuthRequired(Exception):
    """Le dépôt est privé et aucun jeton GitHub valide n'a été fourni :
    impossible de distinguer "pas de mise à jour" de "accès refusé",
    donc on le signale explicitement plutôt que de dire silencieusement
    que la version est à jour."""


def check_for_update(github_token: str = "") -> dict | None:
    """Retourne les infos de la release si une mise à jour est disponible,
    sinon None. Lève requests.RequestException en cas d'échec réseau/API,
    et PrivateRepoAuthRequired si le dépôt répond 404 sans jeton configuré
    (laissé à l'appelant de décider comment l'afficher)."""
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    resp = requests.get(API_LATEST_RELEASE, headers=headers, timeout=TIMEOUT)
    if resp.status_code == 404:
        if not github_token:
            raise PrivateRepoAuthRequired(
                "Le dépôt F4JCF/STATION-SDR est privé : renseignez un jeton "
                "GitHub dans Paramètres pour que la vérification de mise à "
                "jour fonctionne."
            )
        # Jeton fourni mais toujours 404 : probablement aucune release
        # publiée pour l'instant.
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


UPDATE_LOG_PATH = os.path.join(tempfile.gettempdir(), "station_sdr_update.log")
UPDATE_FAILURE_MARKER = os.path.join(tempfile.gettempdir(), "station_sdr_update_failed.txt")


def read_and_clear_failure_marker() -> str | None:
    """Si une mise à jour précédente a échoué à remplacer l'exécutable
    (ex. verrouillé par un antivirus, dossier protégé en écriture), le
    script de relancement laisse ce marqueur. On le lit une fois au
    démarrage pour prévenir l'utilisateur au lieu de rester silencieux,
    puis on le supprime pour ne pas re-signaler le même échec en boucle."""
    try:
        with open(UPDATE_FAILURE_MARKER, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None
    try:
        os.remove(UPDATE_FAILURE_MARKER)
    except OSError:
        pass
    return content


def apply_update_and_restart(new_exe_path: str) -> None:
    """Remplace l'exécutable courant par le nouveau et relance l'application.

    On ne peut pas remplacer un .exe pendant qu'il tourne sous Windows,
    donc on délègue le remplacement à un script batch lancé en détaché,
    qui attend que ce process se termine, remplace le fichier (avec
    plusieurs tentatives si l'ancien .exe est encore brièvement verrouillé
    ou qu'un antivirus retient momentanément le nouveau), relance
    l'exécutable, puis se supprime lui-même.

    Le script journalise chaque étape (horodatée) dans UPDATE_LOG_PATH :
    comme ce processus Python se termine juste après avoir lancé le
    script, c'est la seule manière de diagnostiquer un échec ultérieur
    (antivirus, permissions...) qu'on ne peut pas reproduire depuis un
    environnement de développement Linux.
    """
    current_exe = sys.executable
    pid = os.getpid()

    # Le script relance l'exécutable dans tous les cas, même si le
    # remplacement échoue au bout de toutes les tentatives : l'utilisateur
    # retrouve au moins l'application ouverte plutôt que rien, et un
    # marqueur d'échec est laissé pour qu'on puisse le signaler au
    # prochain démarrage plutôt que de rester silencieux.
    batch_content = f"""@echo off
set "LOG={UPDATE_LOG_PATH}"
set "MARKER={UPDATE_FAILURE_MARKER}"
echo [%date% %time%] Debut (attente PID {pid}, {new_exe_path} -^> {current_exe}) > "%LOG%"

:wait
tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL
if not errorlevel 1 (
    timeout /t 1 /nobreak >NUL
    goto wait
)
echo [%date% %time%] Ancien processus termine >> "%LOG%"

set tries=0
:trymove
move /Y "{new_exe_path}" "{current_exe}" >> "%LOG%" 2>&1
if errorlevel 1 (
    set /a tries+=1
    echo [%date% %time%] Echec du remplacement, tentative %tries%/15 >> "%LOG%"
    if %tries% LSS 15 (
        timeout /t 1 /nobreak >NUL
        goto trymove
    )
    echo [%date% %time%] Abandon : remplacement impossible apres 15 tentatives >> "%LOG%"
    echo Remplacement de "{current_exe}" par "{new_exe_path}" impossible apres 15 tentatives. > "%MARKER%"
    echo Voir le journal complet : "{UPDATE_LOG_PATH}" >> "%MARKER%"
) else (
    echo [%date% %time%] Remplacement reussi >> "%LOG%"
)

echo [%date% %time%] Relancement de "{current_exe}" >> "%LOG%"
start "" "{current_exe}"
echo [%date% %time%] Script termine >> "%LOG%"
del "%~f0"
"""
    batch_path = os.path.join(tempfile.gettempdir(), "station_sdr_update.bat")
    # "ascii" plante (UnicodeEncodeError) dès qu'un chemin contient un
    # caractère accentué (nom d'utilisateur Windows, dossier...), ce qui
    # empêchait silencieusement tout relancement. "mbcs" est l'encodage
    # ANSI natif de Windows (plateforme cible de cet exécutable) et
    # errors="replace" garantit qu'on écrit toujours le script plutôt
    # que de lever une exception ici.
    with open(batch_path, "w", encoding="mbcs", errors="replace") as f:
        f.write(batch_content)

    # CREATE_NO_WINDOW seul : un script détaché qui attend la mort d'un
    # process puis remplace/relance un .exe ressemble, combiné à
    # DETACHED_PROCESS, à un schéma que certains antivirus traitent avec
    # plus de suspicion (comportement de type "auto-réplication"). On
    # reste sur le réglage le plus courant et le moins susceptible d'être
    # bloqué : une fois ce process Python terminé (os._exit ci-dessous),
    # le script devient de toute façon orphelin et continue de tourner.
    subprocess.Popen(
        ["cmd", "/c", batch_path],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    # sys.exit(0) ne suffit pas ici : cette fonction est appelée depuis un
    # callback Tkinter (via self.after). Tkinter intercepte en interne les
    # exceptions levées dans un callback — SystemExit y compris — et les
    # route vers son propre gestionnaire d'erreurs au lieu de les laisser
    # remonter, donc la boucle d'événements (mainloop) ne s'arrête jamais :
    # l'appli restait ouverte indéfiniment, et le script batch attendait
    # pour rien la fin d'un processus qui ne se terminait pas. os._exit()
    # arrête le processus immédiatement au niveau du système d'exploitation,
    # sans passer par la gestion d'exceptions de Python/Tkinter.
    os._exit(0)
