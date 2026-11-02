"""Téléchargement des tuiles de carte.

La bibliothèque tkintermapview s'identifie auprès des serveurs de tuiles
avec un nom générique (« TkinterMapView ») qu'OpenStreetMap bloque
désormais (« Access blocked », erreur 403). Sa politique d'usage demande
que chaque application s'identifie par son propre nom et mette les tuiles
en cache : c'est ce que fait ce module, en remplaçant la fonction de
téléchargement de la bibliothèque.

Fonds proposés : OpenStreetMap, Plan IGN, photos aériennes IGN
(Géoplateforme, gratuits et sans clé).
"""

import hashlib
import io
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import requests

from .version import __version__

USER_AGENT = f"STATION-SDR/{__version__} (F4JCF amateur radio station; +https://github.com/F4JCF/STATION-SDR)"
CACHE_DAYS = 14

STYLES = {
    "osm": {
        "label": "OpenStreetMap",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "max_zoom": 19,
        "credit": "© contributeurs OpenStreetMap",
    },
    "ign_plan": {
        "label": "Plan IGN",
        "url": ("https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
                "&LAYER=GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2&STYLE=normal&TILEMATRIXSET=PM&FORMAT=image/png"
                "&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}"),
        "max_zoom": 19,
        "credit": "© IGN – Géoplateforme",
    },
    "ign_ortho": {
        "label": "Photos aériennes IGN",
        "url": ("https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
                "&LAYER=ORTHOIMAGERY.ORTHOPHOTOS&STYLE=normal&TILEMATRIXSET=PM&FORMAT=image/jpeg"
                "&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}"),
        "max_zoom": 19,
        "credit": "© IGN – Géoplateforme",
    },
}

_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT
_lock = threading.Lock()


def _cache_dir() -> Path:
    from .plat import cache_dir
    path = cache_dir() / "tiles"
    path.mkdir(parents=True, exist_ok=True)
    return path


class _Response:
    def __init__(self, data: bytes, status: int = 200):
        self.content = data
        self.status_code = status
        self.raw = io.BytesIO(data)


def _get(url, stream=False, headers=None, timeout=15, **kwargs):
    """Remplace requests.get dans tkintermapview : bon User-Agent, cache
    disque, et délai maximal (la bibliothèque n'en met aucun)."""
    is_tile = "{" not in url and ("openstreetmap" in url or "geopf.fr" in url or "/tile" in url)
    cache_file = None
    if is_tile:
        try:
            cache_file = _cache_dir() / (hashlib.sha1(url.encode()).hexdigest() + ".img")
            if cache_file.exists() and time.time() - cache_file.stat().st_mtime < CACHE_DAYS * 86400:
                return _Response(cache_file.read_bytes())
        except OSError:
            cache_file = None
    resp = _session.get(url, timeout=timeout, **kwargs)
    data = resp.content
    if resp.status_code == 200 and cache_file is not None and resp.headers.get("Content-Type", "").startswith("image"):
        try:
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, cache_file)
        except OSError:
            pass
    return _Response(data, resp.status_code)


class _DeferredTile:
    """Tuile téléchargée par un fil d'arrière-plan : l'image Tk n'est créée
    qu'au moment de l'afficher, dans le fil principal. (tkintermapview crée
    ses ImageTk.PhotoImage dans ses fils de téléchargement, ce qui n'est pas
    sûr avec Tk et faisait planter l'appli, notamment sur ARM.)"""
    __slots__ = ("pil", "tk")

    def __init__(self, pil):
        self.pil = pil
        self.tk = None

    def resolve(self):
        if self.tk is None:
            from PIL import ImageTk
            self.tk = ImageTk.PhotoImage(self.pil)
            self.pil = None
        return self.tk


class _ImageTkShim:
    """Remplace ImageTk dans tkintermapview : hors du fil principal,
    PhotoImage renvoie une tuile différée."""

    def __getattr__(self, name):
        from PIL import ImageTk
        return getattr(ImageTk, name)

    @staticmethod
    def PhotoImage(image=None, *args, **kwargs):  # noqa: N802 (nom de l'API PIL)
        from PIL import ImageTk
        if image is None or args or kwargs or threading.current_thread() is threading.main_thread():
            return ImageTk.PhotoImage(image, *args, **kwargs)
        return _DeferredTile(image)


def install() -> None:
    """À appeler une fois au démarrage, avant de créer les cartes."""
    try:
        import tkintermapview.map_widget as mw
        mw.requests = SimpleNamespace(get=_get, exceptions=requests.exceptions)
        mw.ImageTk = _ImageTkShim()
        import tkintermapview.canvas_tile as ct
        if not getattr(ct.CanvasTile.draw, "_station_sdr", False):
            orig_draw = ct.CanvasTile.draw

            def draw(self, image_update=False):
                if isinstance(self.image, _DeferredTile):
                    self.image = self.image.resolve()
                return orig_draw(self, image_update)
            draw._station_sdr = True
            ct.CanvasTile.draw = draw
    except Exception:
        pass
    try:
        import tkintermapview.offline_loading as ol
        ol.requests = SimpleNamespace(get=_get, exceptions=requests.exceptions)
    except Exception:
        pass
