"""Différences Windows / Linux : dossiers, son, démarrage automatique,
raccourci de menu et remplacement de l'exécutable lors d'une mise à jour.
"""

import io
import math
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

APP = "STATION-SDR"
WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")


# ----------------------------------------------------------- Dossiers --

def config_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / APP


def cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / APP
    xdg = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(xdg) / APP


def linux_asset_name() -> str:
    m = platform.machine().lower()
    arch = "arm64" if m in ("aarch64", "arm64") else "x86_64" if m in ("x86_64", "amd64") else m
    return f"{APP}-linux-{arch}"


# --------------------------------------------------------------- Son --

_player = None


def _player_cmd(path):
    for exe, args in (("paplay", []), ("pw-play", []), ("aplay", ["-q"]),
                      ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"])):
        if shutil.which(exe):
            return [exe, *args, path]
    return None


def play_wav(data: bytes, wait: bool = False) -> bool:
    """Joue un WAV (octets). Windows : winsound ; Linux : paplay / aplay."""
    global _player
    if WINDOWS:
        try:
            import winsound
            winsound.PlaySound(data, winsound.SND_MEMORY)  # bloquant : appeler depuis un thread
            return True
        except Exception:
            return False
    path = os.path.join(tempfile.gettempdir(), f"station_sdr_{os.getpid()}_{id(data)}.wav")
    try:
        with open(path, "wb") as f:
            f.write(data)
    except OSError:
        return False
    cmd = _player_cmd(path)
    if not cmd:
        return False
    stop_sound()
    try:
        _player = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if wait:
            _player.wait()
        return True
    except OSError:
        return False


def stop_sound():
    global _player
    if WINDOWS:
        try:
            import winsound
            winsound.PlaySound(None, 0)
        except Exception:
            pass
        return
    if _player is not None and _player.poll() is None:
        try:
            _player.terminate()
        except OSError:
            pass
    _player = None


def _alert_wav() -> bytes:
    rate, out = 22050, bytearray()
    for _ in range(3):
        for freq in (1200, 800):
            for i in range(int(rate * 0.25)):
                env = min(1.0, i / 200, (rate * 0.25 - i) / 200)
                out += struct.pack("<h", int(14000 * env * math.sin(2 * math.pi * freq * i / rate)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(out))
    return buf.getvalue()


def alert_sound() -> bool:
    """Sonnerie d'alerte (à appeler depuis un thread)."""
    if WINDOWS:
        try:
            import winsound
            for _ in range(3):
                winsound.Beep(1200, 250)
                winsound.Beep(800, 250)
            return True
        except Exception:
            return False
    return play_wav(_alert_wav(), wait=True)


# ------------------------------------------- Linux : bureau / démarrage --

def _desktop_entry(extra_args="") -> str:
    icon = Path.home() / ".local/share/icons/hicolor/256x256/apps/station-sdr.png"
    return ("[Desktop Entry]\nType=Application\nName=STATION-SDR\n"
            "Comment=Supervision de la station F4JCF-SDR-STATION\n"
            f"Exec=\"{sys.executable}\"{extra_args}\nIcon={icon}\nTerminal=false\n"
            "Categories=HamRadio;Network;Utility;\nStartupWMClass=STATION-SDR\n")


def install_menu_entry(logo_png: str) -> None:
    """Raccourci dans le menu des applications (exécutable Linux seulement)."""
    if not (LINUX and getattr(sys, "frozen", False)):
        return
    try:
        icon_dir = Path.home() / ".local/share/icons/hicolor/256x256/apps"
        icon_dir.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import Image
            Image.open(logo_png).convert("RGBA").resize((256, 256)).save(icon_dir / "station-sdr.png")
        except Exception:
            shutil.copy(logo_png, icon_dir / "station-sdr.png")
        apps = Path.home() / ".local/share/applications"
        apps.mkdir(parents=True, exist_ok=True)
        target = apps / "station-sdr.desktop"
        content = _desktop_entry()
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            target.write_text(content, encoding="utf-8")
    except OSError:
        pass


def _autostart_file() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "autostart" / "station-sdr.desktop"


def linux_autostart_enabled() -> bool:
    return _autostart_file().exists()


def linux_set_autostart(enable: bool) -> None:
    f = _autostart_file()
    if enable:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_desktop_entry(" --minimized"), encoding="utf-8")
    else:
        try:
            f.unlink()
        except FileNotFoundError:
            pass


# --------------------------------------------- Linux : mise à jour --

def linux_update_dest() -> str:
    """Le nouveau binaire est téléchargé à côté de l'actuel (même disque :
    le remplacement est alors instantané et sûr)."""
    exe_dir = os.path.dirname(sys.executable)
    if os.access(exe_dir, os.W_OK):
        return os.path.join(exe_dir, f".{os.path.basename(sys.executable)}.new")
    return os.path.join(tempfile.gettempdir(), "station_sdr_new")


def linux_apply_update_and_restart(new_path: str) -> None:
    """Sous Linux, on peut remplacer un exécutable en cours d'utilisation :
    remplacement direct, puis relance avec un environnement propre."""
    current = sys.executable
    os.chmod(new_path, 0o755)
    if os.path.dirname(os.path.abspath(new_path)) == os.path.dirname(os.path.abspath(current)):
        os.replace(new_path, current)
    else:
        tmp = current + ".new"
        shutil.copyfile(new_path, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, current)
        try:
            os.remove(new_path)
        except OSError:
            pass
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("_PYI", "_MEI"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        for var in ("LD_LIBRARY_PATH", "PATH"):
            if var in env:
                env[var] = os.pathsep.join(p for p in env[var].split(os.pathsep) if not p.startswith(meipass))
        if env.get("LD_LIBRARY_PATH_ORIG"):
            env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    subprocess.Popen([current], env=env, start_new_session=True, cwd=os.path.dirname(current) or None,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os._exit(0)
