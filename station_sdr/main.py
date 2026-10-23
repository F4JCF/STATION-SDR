"""STATION-SDR - Tableau de bord Windows pour le Raspberry Pi F4JCF-SDR-STATION.

Interface Tkinter : bandeau d'en-tête, puces de statut global, écran
d'accueil en vignettes cliquables (météo, trafic aérien, APRS, Winlink,
récepteurs SDR, services) ouvrant chacune un module dédié, rafraîchis
périodiquement en arrière-plan, plus mise à jour automatique au démarrage.
"""

import io
import json
import math
from datetime import datetime
import re
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from collections import deque
from tkinter import filedialog, messagebox, simpledialog, ttk

import requests
from PIL import Image, ImageDraw, ImageTk
from tkintermapview import TkinterMapView

# tkintermapview (superposition de calque radar/satellite) utilise encore
# l'alias historique Image.ANTIALIAS, supprimé par Pillow 10+. Sans ce
# correctif, chaque tuile avec calque levait une AttributeError que la
# bibliothèque rattrape en silence en renvoyant une tuile VIDE — y compris
# le fond de carte, pas seulement le calque — d'où une carte entièrement
# blanche dès qu'un calque (radar/satellite) était activé.
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.LANCZOS

from . import api_client, theme, updater
from .config import load_config, save_config
from .version import __version__
from .aircraft_db import RouteCache, TarDb
from . import aprs_parse, map_tiles, sat_predict
from .sater_ui import SaterMixin
from .icao_ranges import icao_country

APP_TITLE = f"STATION-SDR v{__version__}"


def _resource_path(relative_path: str) -> str:
    """Chemin vers un fichier embarqué (ex. assets/logo.png), qu'on tourne
    en développement ou dans l'exécutable PyInstaller onefile (qui extrait
    ses données dans un dossier temporaire désigné par sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)


CRASH_LOG_PATH = os.path.join(tempfile.gettempdir(), "station_sdr_crash.log")


def _log_crash(tb_text: str, context: str) -> None:
    """Journalise une erreur non affichée nulle part d'autre — build
    --windowed (pas de console), donc toute exception non rattrapée est
    normalement totalement invisible pour l'utilisateur. Utilisé à la fois
    pour un plantage au tout début du démarrage et pour une exception
    levée dans un callback Tkinter (bind/after/command=), que Tkinter
    attrape et route en interne sans jamais la faire remonter (c'est ce
    même mécanisme qui a longtemps masqué le bug du redémarrage auto)."""
    try:
        with open(CRASH_LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ({context}) ===\n{tb_text}\n")
    except OSError:
        pass


# Coordonnées connues des stations météo (code OACI -> lat, lon), avec un
# repli sur Mougins si le code renvoyé par l'API n'est pas reconnu.
STATION_COORDS = {
    "LFMD": (43.5417, 6.9533),  # Cannes - Mandelieu
    "LFMN": (43.6584, 7.2159),  # Nice - Côte d'Azur
}
DEFAULT_COORDS = (43.6045, 6.9971)  # Mougins

# API publique et gratuite RainViewer (radar pluie / satellite en direct,
# sans clé requise) — https://www.rainviewer.com/api.html
RAINVIEWER_INDEX_URL = "https://api.rainviewer.com/public/weather-maps.json"
RAINVIEWER_TILE_HOST = "https://tilecache.rainviewer.com"
RAINVIEWER_MAX_ZOOM = 7  # zoom maximal documenté par RainViewer pour ses tuiles radar
WEATHER_MAP_DEFAULT_ZOOM = 11

# (clé, titre, sous-titre) des vignettes de l'écran d'accueil.
HOME_TILES = [
    ("weather", "Météo", "Relevé METAR local"),
    ("aircraft", "Trafic aérien", "ADS-B / readsb"),
    ("aprs", "APRS", "iGate LoRa + VHF 144,800"),
    ("ais", "AIS", "Trafic maritime"),
    ("beacon", "Balises de détresse", "406 MHz · 121,5 MHz · goniométrie"),
    ("s433", "Capteurs 433 MHz", "Stations météo, sondes, compteurs"),
    ("sats", "Satellites", "Passages ISS et satellites amateurs"),
    ("winlink", "Winlink", "Messagerie radio par paquet"),
    ("sdr", "Clé SDR", "Pilotage et partage automatique"),
    ("svxlink", "SvxLink", "Relais vocal / Echolink"),
    ("openwebrx", "OpenWebRX", "Réception SDR (ouvre le navigateur)"),
    ("services", "Services système", "Vue d'ensemble des services"),
    ("health", "Santé du Pi", "Alimentation, température, ressources"),
    ("alerts", "Alertes", "Urgences avion, balises, messages"),
    ("stats", "Statistiques", "Historique, portée, polaires"),
]

# Module -> mode de la clé SDR unique (sdr_switch.py sur le Pi). Ouvrir
# l'une de ces pages bascule la clé vers ce module, ce qui arrête
# automatiquement le module qui l'utilisait.
MODULE_SDR_MODE = {
    "aircraft": "adsb",
    "ais": "ais",
    "aprs": "aprs",
    "openwebrx": "openwebrx",
    "beacon": "beacon",
    "s433": "433",
}
SDR_MODE_LABELS = {"adsb": "ADS-B", "ais": "AIS", "aprs": "APRS", "openwebrx": "OpenWebRX", "beacon": "Balise 406",
                   "433": "Capteurs 433", "iss": "APRS ISS", "elt121": "Balise 121,5", "off": "arrêtée"}

# Vignettes qui n'ouvrent pas une page interne mais une page web externe
# (le Pi héberge directement l'interface, inutile de la reproduire ici).
EXTERNAL_TILES = {
    "openwebrx": lambda cfg: f"http://{cfg.get('pi_host', '192.168.1.229')}:{cfg.get('port_openwebrx', 8073)}/",
}


def _kt_to_kmh(value):
    try:
        return round(float(value) * 1.852, 1)
    except (TypeError, ValueError):
        return None


def _fmt(value):
    return value if value not in (None, "") else "—"


def _haversine_nm(lat1, lon1, lat2, lon2):
    """Distance orthodromique en milles nautiques entre deux points."""
    try:
        lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    except (TypeError, ValueError):
        return None
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a)) / 1.852  # km -> nm


_SOURCE_LABELS = {
    "adsb_icao": "ADS-B", "adsb_icao_nt": "ADS-B", "adsb_other": "ADS-B",
    "adsr_icao": "ADS-R", "adsr_other": "ADS-R",
    "tisb_icao": "TIS-B", "tisb_other": "TIS-B", "tisb_trackfile": "TIS-B",
    "adsc": "ADS-C", "mlat": "MLAT", "mode_s": "Mode S", "other": "Autre",
}


def _source_label(type_code):
    if not type_code:
        return "—"
    return _SOURCE_LABELS.get(type_code, type_code)


_PLANE_ICON_CACHE = {}


# Échelle de couleurs d'altitude de tar1090 (teinte interpolée selon
# l'altitude, luminosité selon la teinte) pour retrouver les mêmes couleurs.
_ALT_HUE = [(0, 20), (2000, 32.5), (4000, 43), (6000, 54), (8000, 72), (9000, 85), (11000, 140),
            (40000, 300), (51000, 360)]
_HUE_LIGHT = [(0, 53), (20, 50), (32, 54), (40, 52), (46, 51), (50, 46), (60, 43), (80, 41), (100, 41),
              (120, 41), (140, 41), (160, 40), (180, 40), (190, 44), (198, 50), (200, 58), (220, 58),
              (240, 58), (255, 55), (266, 55), (270, 58), (280, 58), (290, 47), (300, 43), (310, 48),
              (320, 48), (340, 52), (360, 53)]


def _interp(table, x):
    if x <= table[0][0]:
        return table[0][1]
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return table[-1][1]


def _hsl_hex(h, s, l):
    import colorsys
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, l / 100, s / 100)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def _alt_color(alt_ft):
    """Couleur d'un avion selon son altitude, identique à tar1090."""
    if alt_ft == "ground":
        return _hsl_hex(220, 0, 30)
    try:
        alt = float(alt_ft)
    except (TypeError, ValueError):
        return _hsl_hex(0, 0, 75)
    hue = _interp(_ALT_HUE, alt)
    return _hsl_hex(hue, 88, _interp(_HUE_LIGHT, hue))


# Silhouette d'avion (nez vers le haut), coordonnées relatives 0..1.
_PLANE_SHAPE = [(0.50, 0.02), (0.56, 0.11), (0.56, 0.37), (0.96, 0.57), (0.96, 0.65), (0.56, 0.53),
                (0.555, 0.79), (0.70, 0.89), (0.70, 0.95), (0.50, 0.90), (0.30, 0.95), (0.30, 0.89),
                (0.445, 0.79), (0.44, 0.53), (0.04, 0.65), (0.04, 0.57), (0.44, 0.37), (0.44, 0.11)]


def _plane_icon(alt_ft, track, size=26, selected=False):
    """Icône avion colorée par altitude (échelle tar1090) et orientée selon
    la route. Dessinée en grand puis réduite (bords lissés), mise en cache."""
    color = _alt_color(alt_ft)
    try:
        heading = round(float(track) / 5) * 5 % 360
    except (TypeError, ValueError):
        heading = 0
    size = size + 8 if selected else size
    cache_key = (color, heading, size, selected)
    cached = _PLANE_ICON_CACHE.get(cache_key)
    if cached is not None:
        return cached
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pts = [(x * big, y * big) for x, y in _PLANE_SHAPE]
    draw.polygon(pts, fill=color, outline="#ffffff" if selected else "#0b0f17", width=8 if selected else 4)
    img = img.rotate(-heading, resample=Image.BICUBIC).resize((size, size), Image.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    _PLANE_ICON_CACHE[cache_key] = photo
    return photo


def _bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


_FLAG_IMAGES = {}


def _flag_image(code):
    """Petit drapeau (21×14) du pays d'immatriculation ; drapeaux MIT de
    country-flag-icons, repris de tar1090."""
    if not code:
        return None
    code = code.lower()
    if code in _FLAG_IMAGES:
        return _FLAG_IMAGES[code]
    if "_data" not in _FLAG_IMAGES:
        try:
            with open(_resource_path("assets/flags.json"), "r", encoding="utf-8") as f:
                _FLAG_IMAGES["_data"] = json.load(f)
        except Exception:
            _FLAG_IMAGES["_data"] = {}
    b64 = _FLAG_IMAGES["_data"].get(code)
    img = None
    if b64:
        try:
            img = tk.PhotoImage(data=b64)
        except tk.TclError:
            img = None
    _FLAG_IMAGES[code] = img
    return img


def _hex_to_rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


_TILE_ICON_CACHE = {}


def _tile_icon(key: str, size: int = 30, color: str = None):
    """Pictogramme monoligne pour une vignette d'accueil, dessiné à la
    volée (pas de fichier image à gérer/embarquer). Mis en cache par
    (clé, taille, couleur) — le cache est gardé en mémoire pour toute la
    durée du programme, comme pour les icônes avion."""
    color = color or theme.ACCENT
    cache_key = (key, size, color)
    cached = _TILE_ICON_CACHE.get(cache_key)
    if cached is not None:
        return cached

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lw = max(1, round(size / 15))

    def pt(fx, fy):
        return (fx * size, fy * size)

    cx, cy = size * 0.5, size * 0.5

    if key == "weather":
        # Soleil (cercle + rayons) partiellement masqué par un nuage, dans
        # le style classique "éclaircies". Le nuage est rempli avec le fond
        # réel de la vignette pour occulter proprement les rayons dessous
        # (ces icônes ne sont utilisées que sur un fond Card.TFrame).
        sx, sy, sr = size * 0.30, size * 0.30, size * 0.15
        d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], outline=color, width=lw)
        for ang_deg in (0, 45, 90, 135, 180, 225, 270, 315):
            ang = math.radians(ang_deg)
            x1, y1 = sx + (sr + size * 0.05) * math.cos(ang), sy + (sr + size * 0.05) * math.sin(ang)
            x2, y2 = sx + (sr + size * 0.14) * math.cos(ang), sy + (sr + size * 0.14) * math.sin(ang)
            d.line([x1, y1, x2, y2], fill=color, width=lw)

        mask_fill = _hex_to_rgb(theme.BG_CARD) + (255,)
        d.rounded_rectangle(
            [*pt(0.16, 0.52), *pt(0.98, 0.88)], radius=size * 0.16, fill=mask_fill, outline=color, width=lw,
        )
        bx, by, br = size * 0.38, size * 0.50, size * 0.17
        d.ellipse([bx - br, by - br, bx + br, by + br], fill=mask_fill, outline=color, width=lw)
    elif key == "aircraft":
        d.polygon(
            [
                pt(0.50, 0.04), pt(0.62, 0.42), pt(0.96, 0.60), pt(0.96, 0.70),
                pt(0.62, 0.58), pt(0.58, 0.84), pt(0.76, 0.94), pt(0.76, 1.00),
                pt(0.50, 0.90), pt(0.24, 1.00), pt(0.24, 0.94), pt(0.42, 0.84),
                pt(0.38, 0.58), pt(0.04, 0.70), pt(0.04, 0.60), pt(0.38, 0.42),
            ],
            fill=color,
        )
    elif key == "aprs":
        ax, ay = cx, size * 0.82
        d.ellipse([ax - size * 0.07, ay - size * 0.07, ax + size * 0.07, ay + size * 0.07], fill=color)
        for r in (0.20, 0.36, 0.52):
            rad = size * r
            d.arc([ax - rad, ay - rad, ax + rad, ay + rad], start=200, end=340, fill=color, width=lw)
    elif key == "ais":
        d.polygon([pt(0.08, 0.72), pt(0.92, 0.72), pt(0.76, 0.92), pt(0.24, 0.92)], outline=color, width=lw)
        d.line([*pt(0.5, 0.72), *pt(0.5, 0.10)], fill=color, width=lw)
        d.polygon([pt(0.5, 0.10), pt(0.5, 0.40), pt(0.80, 0.26)], fill=color)
    elif key == "winlink":
        d.rectangle([*pt(0.06, 0.22), *pt(0.94, 0.78)], outline=color, width=lw)
        d.line([*pt(0.06, 0.24), *pt(0.5, 0.56)], fill=color, width=lw)
        d.line([*pt(0.94, 0.24), *pt(0.5, 0.56)], fill=color, width=lw)
    elif key == "sdr":
        d.line([*pt(0.5, 0.05), *pt(0.5, 0.95)], fill=color, width=lw)
        d.line([*pt(0.18, 0.95), *pt(0.82, 0.95)], fill=color, width=lw)
        d.line([*pt(0.5, 0.05), *pt(0.22, 0.32)], fill=color, width=lw)
        d.line([*pt(0.5, 0.05), *pt(0.78, 0.32)], fill=color, width=lw)
        d.line([*pt(0.5, 0.38), *pt(0.30, 0.58)], fill=color, width=lw)
        d.line([*pt(0.5, 0.38), *pt(0.70, 0.58)], fill=color, width=lw)
    elif key == "svxlink":
        d.rounded_rectangle([*pt(0.34, 0.06), *pt(0.66, 0.58)], radius=size * 0.14, outline=color, width=lw)
        d.arc([*pt(0.18, 0.28), *pt(0.82, 0.78)], start=20, end=160, fill=color, width=lw)
        d.line([*pt(0.5, 0.72), *pt(0.5, 0.92)], fill=color, width=lw)
        d.line([*pt(0.30, 0.92), *pt(0.70, 0.92)], fill=color, width=lw)
    elif key == "openwebrx":
        d.rounded_rectangle([*pt(0.05, 0.10), *pt(0.95, 0.72)], radius=size * 0.06, outline=color, width=lw)
        d.line([*pt(0.5, 0.72), *pt(0.5, 0.88)], fill=color, width=lw)
        d.line([*pt(0.28, 0.92), *pt(0.72, 0.92)], fill=color, width=lw)
        d.line(
            [pt(0.14, 0.45), pt(0.28, 0.28), pt(0.40, 0.54), pt(0.54, 0.24), pt(0.68, 0.50), pt(0.86, 0.38)],
            fill=color, width=max(1, lw - 1),
        )
    elif key == "beacon":
        # Balise : boîtier, antenne et ondes
        d.rounded_rectangle([*pt(0.36, 0.40), *pt(0.64, 0.92)], radius=size * 0.05, outline=color, width=lw)
        d.line([pt(0.5, 0.40), pt(0.5, 0.14)], fill=color, width=lw)
        d.ellipse([*pt(0.44, 0.62), *pt(0.56, 0.74)], fill=color)
        d.arc([*pt(0.22, 0.02), *pt(0.78, 0.40)], start=200, end=340, fill=color, width=lw)
        d.arc([*pt(0.06, -0.10), *pt(0.94, 0.52)], start=210, end=330, fill=color, width=lw)
    elif key == "s433":
        # thermomètre + ondes
        d.rounded_rectangle([*pt(0.40, 0.08), *pt(0.56, 0.66)], radius=size * 0.08, outline=color, width=lw)
        d.ellipse([*pt(0.34, 0.62), *pt(0.62, 0.92)], outline=color, width=lw)
        d.arc([*pt(0.58, 0.18), *pt(0.86, 0.54)], start=300, end=60, fill=color, width=lw)
        d.arc([*pt(0.64, 0.08), *pt(1.02, 0.64)], start=300, end=60, fill=color, width=lw)
    elif key == "sats":
        # satellite : corps + panneaux + orbite
        d.rectangle([*pt(0.42, 0.40), *pt(0.58, 0.60)], outline=color, width=lw)
        d.rectangle([*pt(0.10, 0.44), *pt(0.36, 0.56)], outline=color, width=lw)
        d.rectangle([*pt(0.64, 0.44), *pt(0.90, 0.56)], outline=color, width=lw)
        d.arc([*pt(0.04, 0.14), *pt(0.96, 0.86)], start=200, end=340, fill=color, width=max(1, lw - 1))
    elif key == "stats":
        d.line([pt(0.10, 0.90), pt(0.92, 0.90)], fill=color, width=lw)
        d.line([pt(0.10, 0.10), pt(0.10, 0.90)], fill=color, width=lw)
        d.line([pt(0.18, 0.72), pt(0.38, 0.48), pt(0.56, 0.60), pt(0.86, 0.22)], fill=color, width=lw)
    elif key == "alerts":
        d.polygon([pt(0.5, 0.06), pt(0.95, 0.90), pt(0.05, 0.90)], outline=color, width=lw)
        d.line([pt(0.5, 0.34), pt(0.5, 0.64)], fill=color, width=lw + 1)
        d.ellipse([*pt(0.46, 0.72), *pt(0.54, 0.80)], fill=color)
    elif key == "health":
        # Cœur + tracé d'électrocardiogramme
        d.polygon(
            [pt(0.5, 0.88), pt(0.12, 0.50), pt(0.10, 0.30), pt(0.22, 0.14), pt(0.38, 0.14), pt(0.5, 0.28),
             pt(0.62, 0.14), pt(0.78, 0.14), pt(0.90, 0.30), pt(0.88, 0.50)],
            outline=color, width=lw,
        )
        d.line(
            [pt(0.18, 0.50), pt(0.36, 0.50), pt(0.44, 0.34), pt(0.54, 0.66), pt(0.62, 0.50), pt(0.82, 0.50)],
            fill=color, width=max(1, lw - 1),
        )
    elif key == "services":
        teeth = 8
        outer_r, inner_r, hole_r = size * 0.42, size * 0.30, size * 0.14
        pts = []
        for t in range(teeth * 2):
            ang = math.pi * t / teeth
            r = outer_r if t % 2 == 0 else inner_r
            pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
        d.polygon(pts, outline=color, width=lw)
        d.ellipse([cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r], outline=color, width=lw)
    else:
        d.ellipse([*pt(0.08, 0.08), *pt(0.92, 0.92)], outline=color, width=lw)

    photo = ImageTk.PhotoImage(img)
    _TILE_ICON_CACHE[cache_key] = photo
    return photo


def _find_service(data: dict, *name_fragments: str):
    """Cherche un service dans la réponse de station_api.py dont la clé ou
    le libellé contient un des fragments donnés (insensible à la casse) —
    utilisé pour des services dont on ne connaît pas la clé exacte côté Pi
    (AIS, SvxLink), sans faire planter l'app si elle diffère."""
    fragments = [f.lower() for f in name_fragments]
    for key, info in data.items():
        label = str(info.get("label", key)).lower()
        key_l = key.lower()
        if any(f in key_l or f in label for f in fragments):
            return info
    return None


class _LineChart(tk.Canvas):
    """Petite courbe temporelle (une série), avec trous quand une valeur
    manque (module sans la clé) et bandes rouges optionnelles (périodes de
    sous-tension)."""

    def __init__(self, parent, title, unit, color, width=420, height=190):
        super().__init__(parent, width=width, height=height, bg=theme.BG_CARD, highlightthickness=0)
        self.title, self.unit, self.color = title, unit, color
        self.points, self.bands, self.note = [], [], ""
        self.t0 = self.t1 = None
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, points, t0, t1, bands=None, note=""):
        self.points, self.t0, self.t1 = points, t0, t1
        self.bands, self.note = bands or [], note
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = max(self.winfo_width(), 200), max(self.winfo_height(), 120)
        left, right, top, bottom = 44, 12, 26, 26
        vals = [v for _t, v in self.points if v is not None]
        last = next((v for _t, v in reversed(self.points) if v is not None), None)
        head = self.title + (f"   ·   max {max(vals):.0f} {self.unit}   ·   actuel {last:.0f} {self.unit}"
                             if vals and last is not None else "")
        self.create_text(8, 10, text=head, anchor="w", fill=theme.TEXT, font=("Segoe UI", 9, "bold"))
        pw, ph = w - left - right, h - top - bottom
        self.create_rectangle(left, top, left + pw, top + ph, outline=theme.BORDER)
        if not self.t0 or not self.t1 or self.t1 <= self.t0:
            return
        ymax = max(vals) if vals else 1
        ymax = max(1, ymax * 1.15)
        span = self.t1 - self.t0

        def X(t):
            return left + (t - self.t0) / span * pw

        def Y(v):
            return top + ph - v / ymax * ph

        for b0, b1 in self.bands:
            self.create_rectangle(X(max(b0, self.t0)), top, X(min(b1, self.t1)), top + ph,
                                  fill="#5a1f22", outline="")
        for i in range(5):
            v = ymax * i / 4
            y = Y(v)
            self.create_line(left, y, left + pw, y, fill=theme.BORDER, dash=(2, 4))
            self.create_text(left - 4, y, text=f"{v:.0f}", anchor="e", fill=theme.MUTED, font=("Segoe UI", 8))
        fmt = "%H:%M" if span <= 2 * 86400 else "%d/%m"
        for i in range(5):
            t = self.t0 + span * i / 4
            self.create_text(X(t), top + ph + 12, text=time.strftime(fmt, time.localtime(t)),
                             fill=theme.MUTED, font=("Segoe UI", 8))
        seg, prev_t = [], None
        gap = max(300, span / 150)
        for t, v in self.points:
            if v is None or (prev_t is not None and t - prev_t > gap):
                if len(seg) >= 4:
                    self.create_line(*seg, fill=self.color, width=2)
                elif len(seg) == 2:
                    self.create_oval(seg[0] - 2, seg[1] - 2, seg[0] + 2, seg[1] + 2, fill=self.color, outline="")
                seg = []
            if v is not None:
                seg += [X(t), Y(v)]
            prev_t = t
        if len(seg) >= 4:
            self.create_line(*seg, fill=self.color, width=2)
        elif len(seg) == 2:
            self.create_oval(seg[0] - 2, seg[1] - 2, seg[0] + 2, seg[1] + 2, fill=self.color, outline="")
        if not vals:
            self.create_text(left + pw / 2, top + ph / 2, text=self.note or "aucune donnée sur la période",
                             fill=theme.MUTED, font=theme.FONT_SUBTITLE)


class _PolarChart(tk.Canvas):
    """Portée maximale par direction (secteurs de 10°), nord en haut."""

    def __init__(self, parent, title, color, size=330):
        super().__init__(parent, width=size, height=size + 24, bg=theme.BG_CARD, highlightthickness=0)
        self.title, self.color, self.size = title, color, size
        self.sectors = [None] * 36
        self._draw()

    def set(self, sectors):
        self.sectors = sectors or [None] * 36
        self._draw()

    def _draw(self):
        self.delete("all")
        s = self.size
        cx, cy, r = s / 2, s / 2 + 22, s / 2 - 26
        vals = [v for v in self.sectors if v]
        vmax = max(vals) if vals else 0
        self.create_text(8, 10, anchor="w", fill=theme.TEXT, font=("Segoe UI", 9, "bold"),
                         text=self.title + (f"   ·   max {vmax:.0f} km" if vmax else ""))
        scale_max = max(10, math.ceil(vmax / 50) * 50) if vmax else 50
        for i in range(1, 5):
            rr = r * i / 4
            self.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=theme.BORDER)
            self.create_text(cx + 3, cy - rr + 7, text=f"{scale_max * i / 4:.0f}", anchor="w",
                             fill=theme.MUTED, font=("Segoe UI", 7))
        for ang, lab in ((0, "N"), (90, "E"), (180, "S"), (270, "O")):
            a = math.radians(ang)
            self.create_line(cx, cy, cx + r * math.sin(a), cy - r * math.cos(a), fill=theme.BORDER)
            self.create_text(cx + (r + 11) * math.sin(a), cy - (r + 11) * math.cos(a), text=lab,
                             fill=theme.MUTED, font=("Segoe UI", 8, "bold"))
        if not vals:
            self.create_text(cx, cy, text="aucune donnée", fill=theme.MUTED, font=theme.FONT_SUBTITLE)
            return
        pts = []
        for i, v in enumerate(self.sectors):
            for frac in (0.0, 1.0):  # bord gauche et droit du secteur
                a = math.radians(i * 10 + frac * 10)
                rr = (v or 0) / scale_max * r
                pts += [cx + rr * math.sin(a), cy - rr * math.cos(a)]
        # Remplissage « transparent » simulé : couleur mélangée au fond (le
        # motif stipple de Tk n'est pas fiable sous Windows).
        c1, c2 = _hex_to_rgb(self.color), _hex_to_rgb(theme.BG_CARD)
        fill = "#%02x%02x%02x" % tuple(int(a * 0.35 + b * 0.65) for a, b in zip(c1, c2))
        self.create_polygon(*pts, fill=fill, outline="", width=0)
        for i in range(1, 5):  # cercles redessinés par-dessus le remplissage
            rr = r * i / 4
            self.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=theme.BORDER)
        self.create_polygon(*pts, fill="", outline=self.color, width=2)
        self.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill=theme.TEXT, outline="")


class _Gauge(tk.Canvas):
    """Vu-mètre à aiguille (demi-cercle) avec zones vert / orange / rouge."""

    def __init__(self, parent, title, unit, vmin, vmax, warn, crit, width=210, height=150, bg=None):
        super().__init__(parent, width=width, height=height, bg=bg or theme.BG_CARD, highlightthickness=0)
        self.title, self.unit, self.vmin, self.vmax = title, unit, vmin, vmax
        self.warn, self.crit = warn, crit
        self.w, self.h = width, height
        self.value = None
        self.subtitle = ""
        self._draw()

    def _angle(self, v):
        v = max(self.vmin, min(self.vmax, v))
        return 180 - (v - self.vmin) / (self.vmax - self.vmin) * 180

    def set(self, value, subtitle=""):
        self.value, self.subtitle = value, subtitle
        self._draw()

    def _draw(self):
        self.delete("all")
        cx, cy = self.w / 2, self.h - 40
        r = min(self.w / 2 - 14, self.h - 66)
        box = (cx - r, cy - r, cx + r, cy + r)
        self.create_text(cx, 10, text=self.title, fill=theme.MUTED, font=("Segoe UI", 9, "bold"))
        zones = ((self.vmin, self.warn, theme.GREEN), (self.warn, self.crit, theme.ORANGE), (self.crit, self.vmax, theme.RED))
        for lo, hi, col in zones:
            a0, a1 = self._angle(lo), self._angle(hi)
            self.create_arc(*box, start=a1, extent=a0 - a1, style="arc", outline=col, width=10)
        for i in range(11):  # graduations
            v = self.vmin + (self.vmax - self.vmin) * i / 10
            ang = math.radians(self._angle(v))
            r1, r2 = r - 9, r - (17 if i % 5 == 0 else 13)
            self.create_line(cx + r1 * math.cos(ang), cy - r1 * math.sin(ang),
                             cx + r2 * math.cos(ang), cy - r2 * math.sin(ang), fill=theme.MUTED)
        if self.value is None:
            txt, col = "—", theme.MUTED
        else:
            ang = math.radians(self._angle(self.value))
            self.create_line(cx, cy, cx + (r - 14) * math.cos(ang), cy - (r - 14) * math.sin(ang),
                             fill=theme.TEXT, width=3, capstyle="round")
            col = theme.RED if self.value >= self.crit else theme.ORANGE if self.value >= self.warn else theme.GREEN
            txt = f"{self.value:.0f} {self.unit}"
        self.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill=theme.TEXT, outline="")
        self.create_text(cx, cy + 15, text=txt, fill=col, font=("Segoe UI", 13, "bold"))
        if self.subtitle:
            self.create_text(cx, cy + 31, text=self.subtitle, fill=theme.MUTED, font=("Segoe UI", 8))


class _TileChip:
    """Pastille d'une vignette d'accueil. Les modules qui partagent la clé
    SDR affichent « En pause » quand la clé est ailleurs, et la vignette du
    module qui détient la clé est encadrée de la couleur d'accent ; le texte
    réel envoyé par chaque page est conservé et réaffiché dès que le
    module reprend la clé."""

    def __init__(self, app, key, label, frame):
        self.app, self.key, self.label, self.frame = app, key, label, frame
        self.text = "—"

    def config(self, text=None, **kwargs):
        if text is not None:
            self.text = text
        if kwargs:
            self.label.config(**kwargs)
        self.refresh()

    configure = config

    def refresh(self):
        mode = MODULE_SDR_MODE.get(self.key)
        current = getattr(self.app, "_sdr_mode", None)
        if self.key == "beacon" and current == "elt121":
            mode = "elt121"  # la vignette Balises couvre aussi le 121,5 MHz
        if self.key == "aprs" and mode and current and mode != current:
            # le LoRa (iGate) fonctionne sans la clé SDR : seule la VHF est en pause
            self.label.config(text=f" {self.text} · VHF en pause ", fg=theme.TEXT, bg=theme.BG_CARD_ALT)
            self.frame.config(bg=theme.BORDER)
        elif mode and current and mode != current:
            self.label.config(
                text=(" En pause · clé SDR arrêtée " if current == "off"
                      else f" En pause · clé sur {SDR_MODE_LABELS.get(current, current)} "), fg=theme.MUTED,
                bg=theme.BG_CARD_ALT,
            )
            self.frame.config(bg=theme.BORDER)
        elif self.key == "alerts" and str(self.text).startswith("⚠"):
            self.label.config(text=f" {self.text} ", fg="#0b0f17", bg=theme.RED)
            self.frame.config(bg=theme.RED)
        elif mode and current == mode:
            self.label.config(text=f" ● {self.text} ", fg="#1a1200", bg=theme.ACCENT)
            self.frame.config(bg=theme.ACCENT)
        else:
            self.label.config(text=f" {self.text} ", fg=theme.TEXT, bg=theme.BG_CARD_ALT)
            self.frame.config(bg=theme.BORDER)


class App(SaterMixin, tk.Tk):
    SDR_LABELS = None  # rempli après la définition (libellés des modes)

    def __init__(self):
        super().__init__()
        # Par défaut, Tkinter attrape en interne toute exception levée
        # dans un callback (bind/after/command=...) et se contente de
        # l'imprimer sur stderr — invisible dans un exécutable --windowed
        # (pas de console). On la journalise explicitement à la place,
        # sinon un bug dans un rafraîchissement périodique ou un clic
        # disparaît sans aucune trace.
        self.report_callback_exception = self._on_callback_exception
        self.title(APP_TITLE)
        self.geometry("1040x680")
        self.minsize(860, 560)
        try:
            self.iconbitmap(default=_resource_path("assets/icon.ico"))
        except Exception:
            pass  # icône absente (dev) ou plateforme ne supportant pas .ico

        self.config_data = load_config()
        self._stop_event = threading.Event()
        self._last_weather = {}
        self._last_services = {}

        theme.apply_dark_theme(self)
        map_tiles.install()
        self._all_maps = []
        self._alert_init()
        self._build_menu()
        self._build_header()
        self._build_body()
        self._tick_clock()

        self.after(100, self._check_previous_update_failure)
        self.after(200, self._check_update_on_startup)
        self._start_refresh_loop()

        self.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self._tray_icon = None
        self.after(500, self._start_tray)
        if "--minimized" in sys.argv:
            # Lancé au démarrage de Windows : on reste discrètement près de
            # l'horloge, les alertes continuent d'arriver.
            self.after(800, self._hide_to_tray)

    # ------------------------------------------- Zone de notification --

    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def _autostart_enabled(self):
        if sys.platform != "win32":
            return False
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY) as k:
                winreg.QueryValueEx(k, "STATION-SDR")
            return True
        except OSError:
            return False

    def _toggle_autostart(self):
        want = bool(self._autostart_var.get())
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            messagebox.showinfo("Démarrage avec Windows", "Disponible uniquement avec STATION-SDR.exe sous Windows.")
            self._autostart_var.set(False)
            return
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                if want:
                    winreg.SetValueEx(k, "STATION-SDR", 0, winreg.REG_SZ, f'"{sys.executable}" --minimized')
                else:
                    try:
                        winreg.DeleteValue(k, "STATION-SDR")
                    except OSError:
                        pass
        except OSError as exc:
            messagebox.showerror("Démarrage avec Windows", f"Réglage impossible : {exc}")
            self._autostart_var.set(not want)

    def _toggle_close_to_tray(self):
        self.config_data["close_to_tray"] = bool(self._close_tray_var.get())
        try:
            save_config(self.config_data)
        except Exception:
            pass

    def _start_tray(self):
        """Icône près de l'horloge (Windows). Sans pystray (ou hors
        Windows), l'appli fonctionne simplement sans cette icône."""
        if sys.platform != "win32":
            return
        try:
            import pystray
        except Exception:
            return
        try:
            try:
                ico = Image.open(_resource_path("assets/icon.ico"))
                ico.size = (64, 64)
                image = ico.convert("RGBA")
            except Exception:
                image = Image.open(_resource_path("assets/logo.png")).convert("RGBA").resize((64, 64))
            menu = pystray.Menu(
                pystray.MenuItem("Ouvrir STATION-SDR", lambda *_: self.after(0, self._show_from_tray), default=True),
                pystray.MenuItem("Arrêter la clé SDR", lambda *_: self.after(0, lambda: (self._show_from_tray(), self._stop_sdr()))),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quitter", lambda *_: self.after(0, self._on_close)),
            )
            self._tray_icon = pystray.Icon("STATION-SDR", image, "STATION-SDR — F4JCF", menu)
            self._tray_icon.run_detached()
        except Exception as exc:
            _log_crash(f"Icône de la zone de notification indisponible : {exc}", context="tray")
            self._tray_icon = None

    def _tray_notify(self, title, text):
        if self._tray_icon is not None:
            try:
                self._tray_icon.notify(text[:250], title[:60])
            except Exception:
                pass

    def _hide_to_tray(self):
        if self._tray_icon is None:
            self.iconify()
            return
        self.withdraw()
        if not getattr(self, "_tray_hint_shown", False):
            self._tray_hint_shown = True
            self._tray_notify("STATION-SDR continue en arrière-plan",
                              "Les alertes restent actives. Double-cliquez sur l'icône pour rouvrir la fenêtre.")

    def _show_from_tray(self):
        self.deiconify()
        self.lift()
        try:
            self.focus_force()
        except tk.TclError:
            pass

    def _on_window_close(self):
        if self._tray_icon is not None and self.config_data.get("close_to_tray", True):
            self._hide_to_tray()
        else:
            self._on_close()

    # ------------------------------------------------------- Fonds de carte --

    def _make_map(self, parent):
        """Carte avec le fond choisi (OpenStreetMap, Plan IGN, photos IGN)
        et la mention de source obligatoire en bas à droite."""
        m = TkinterMapView(parent, corner_radius=0)
        credit = tk.Label(parent, text="", bg="#ffffff", fg="#333333", font=("Segoe UI", 7), padx=3)
        credit.place(relx=1.0, rely=1.0, anchor="se", x=-2, y=-2)
        m._credit_label = credit
        self._all_maps.append(m)
        self._apply_map_style(m)
        return m

    def _apply_map_style(self, m):
        style = map_tiles.STYLES.get(self.config_data.get("map_style", "osm"), map_tiles.STYLES["osm"])
        m.set_tile_server(style["url"], max_zoom=style["max_zoom"])
        m._credit_label.config(text=style["credit"])
        m._credit_label.lift()

    def _set_map_style(self, key):
        self.config_data["map_style"] = key
        try:
            save_config(self.config_data)
        except Exception:
            pass
        for m in self._all_maps:
            try:
                self._apply_map_style(m)
            except Exception:
                pass

    # ---------------------------------------------------------------- UI --

    def _build_menu(self):
        menubar = tk.Menu(self)
        app_menu = tk.Menu(menubar, tearoff=0)
        app_menu.add_command(label="Paramètres...", command=self._open_settings)
        app_menu.add_command(label="Vérifier les mises à jour", command=self._manual_update_check)
        app_menu.add_separator()
        self._alert_sound_var = tk.BooleanVar(value=self.config_data.get("alert_sound", True))
        app_menu.add_checkbutton(label="Alertes sonores", variable=self._alert_sound_var,
                                 command=self._toggle_alert_sound)
        app_menu.add_command(label="Historique des alertes", command=self._show_alert_history)
        app_menu.add_separator()
        map_menu = tk.Menu(app_menu, tearoff=0)
        self._map_style_var = tk.StringVar(value=self.config_data.get("map_style", "osm"))
        for key, st in map_tiles.STYLES.items():
            map_menu.add_radiobutton(label=st["label"], value=key, variable=self._map_style_var,
                                     command=lambda k=key: self._set_map_style(k))
        app_menu.add_cascade(label="Fond de carte", menu=map_menu)
        app_menu.add_command(label="Arrêter la clé SDR", command=lambda: self._stop_sdr())
        app_menu.add_separator()
        self._autostart_var = tk.BooleanVar(value=self._autostart_enabled())
        app_menu.add_checkbutton(label="Lancer au démarrage de Windows", variable=self._autostart_var,
                                 command=self._toggle_autostart)
        self._close_tray_var = tk.BooleanVar(value=self.config_data.get("close_to_tray", True))
        app_menu.add_checkbutton(label="Fermer = réduire près de l'horloge", variable=self._close_tray_var,
                                 command=self._toggle_close_to_tray)
        app_menu.add_command(label="Ouvrir le tableau de bord du téléphone",
                             command=lambda: self._open_url(
                                 f"http://{self.config_data.get('pi_host', '192.168.1.229')}:{self.config_data.get('port_monitor', 5008)}/"))
        app_menu.add_separator()
        app_menu.add_command(label="Quitter", command=self._on_close)
        menubar.add_cascade(label="STATION-SDR", menu=app_menu)

        # Liste déroulante de toutes les applications/modules disponibles,
        # pour y accéder directement sans passer par l'écran d'accueil.
        modules_menu = tk.Menu(menubar, tearoff=0)
        modules_menu.add_command(label="Accueil", command=self.show_home)
        modules_menu.add_separator()
        for key, title, _subtitle in HOME_TILES:
            suffix = " (navigateur)" if key in EXTERNAL_TILES else ""
            modules_menu.add_command(label=f"{title}{suffix}", command=lambda k=key: self.show_page(k))
        menubar.add_cascade(label="Modules", menu=modules_menu)

        self.config(menu=menubar)

    def _build_header(self):
        header = tk.Frame(self, bg=theme.BG)
        header.pack(fill="x", padx=20, pady=(16, 8))

        self._header_logo_image = None
        try:
            try:
                # L'icône de l'exe contient le même logo en 256 px : on la
                # réduit (net) plutôt que d'agrandir le PNG de 96 px (flou).
                ico = Image.open(_resource_path("assets/icon.ico"))
                ico.size = (256, 256)
                logo_img = ico.convert("RGBA")
            except Exception:
                logo_img = Image.open(_resource_path("assets/logo.png")).convert("RGBA")
            logo_img = logo_img.resize((190, 190), Image.LANCZOS)
            # Le logo est rond : on masque les coins (blancs) pour qu'il se
            # détache proprement sur le fond sombre.
            mask = Image.new("L", (560, 560), 0)
            ImageDraw.Draw(mask).ellipse([4, 4, 555, 555], fill=255)
            mask = mask.resize((190, 190), Image.LANCZOS)
            alpha = Image.composite(logo_img.getchannel("A"), Image.new("L", (190, 190), 0), mask)
            logo_img.putalpha(alpha)
            self._header_logo_image = ImageTk.PhotoImage(logo_img)
            tk.Label(header, image=self._header_logo_image, bg=theme.BG).pack(side="left", padx=(0, 14))
        except Exception:
            pass  # logo absent/illisible : l'en-tête reste utilisable sans lui

        left = tk.Frame(header, bg=theme.BG)
        left.pack(side="left", fill="y")
        tk.Label(left, text="STATION-SDR", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_TITLE).pack(anchor="w")
        tk.Label(
            left, text="Supervision F4JCF-SDR-STATION — ADS-B · Météo · APRS · SDR",
            bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE,
        ).pack(anchor="w")

        right = tk.Frame(header, bg=theme.BG)
        right.pack(side="right", fill="y")
        self.clock_label = tk.Label(right, text="--:--:--", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_TITLE)
        self.clock_label.pack(anchor="e")
        self.date_label = tk.Label(right, text="", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.date_label.pack(anchor="e")

        # Puces de statut global
        strip = tk.Frame(self, bg=theme.BG)
        strip.pack(fill="x", padx=20, pady=(0, 10))

        self.chip_host = theme.chip(strip, f"Hôte {self.config_data.get('pi_host', '?')}")
        self.chip_host.pack(side="left", padx=(0, 8))
        self.chip_aircraft = theme.chip(strip, "Avions : —")
        self.chip_aircraft.pack(side="left", padx=8)
        self.chip_services = theme.chip(strip, "Services : —")
        self.chip_services.pack(side="left", padx=8)
        self.chip_sdr = theme.chip(strip, "SDR : —")
        self.chip_sdr.pack(side="left", padx=8)
        self.chip_health = theme.chip(strip, "Pi : —")
        self.chip_health.pack(side="left", padx=8)
        self.chip_health.bind("<Button-1>", lambda _e: self.show_page("health"))
        self.chip_alerts = theme.chip(strip, "Alertes : 0")
        self.chip_alerts.pack(side="left", padx=8)
        self.chip_alerts.bind("<Button-1>", lambda _e: self._show_alert_history())
        try:
            self.chip_alerts.configure(cursor="hand2")
            self.chip_health.configure(cursor="hand2")
        except tk.TclError:
            pass
        self.chip_updated = theme.chip(strip, "Maj : —")
        self.chip_updated.pack(side="right")

    def _build_body(self):
        container = tk.Frame(self, bg=theme.BG)
        container.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        nav = tk.Frame(container, bg=theme.BG)
        nav.pack(fill="x", pady=(0, 10))
        self.view_title_label = tk.Label(
            nav, text="Accueil", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION,
        )
        self.view_title_label.pack(side="left")
        self.back_button = ttk.Button(
            nav, text="← Accueil", style="Flat.TButton", command=self.show_home,
        )
        # Pas encore affiché : on est sur l'accueil au démarrage.

        self.page_area = tk.Frame(container, bg=theme.BG)
        self.page_area.pack(fill="both", expand=True)

        self.home_frame = tk.Frame(self.page_area, bg=theme.BG)
        # Une page par vignette, sauf celles listées dans EXTERNAL_TILES
        # (ouvrent une page web externe plutôt qu'une vue interne).
        self.pages = {
            key: tk.Frame(self.page_area, bg=theme.BG)
            for key, _title, _subtitle in HOME_TILES
            if key not in EXTERNAL_TILES
        }

        self._build_home_tiles()
        self._build_weather_tab(self.pages["weather"])
        self._build_aircraft_table(self.pages["aircraft"])
        self._build_aprs_tab(self.pages["aprs"])
        self._build_winlink_tab(self.pages["winlink"])
        self._build_ais_tab(self.pages["ais"])
        self._build_beacon_tab(self.pages["beacon"])
        self._build_s433_tab(self.pages["s433"])
        self._build_sat_tab(self.pages["sats"])
        self._build_svxlink_tab(self.pages["svxlink"])
        self._build_sdr_tab(self.pages["sdr"])
        self._build_health_tab(self.pages["health"])
        self._build_alerts_tab(self.pages["alerts"])
        self._build_stats_tab(self.pages["stats"])
        self._update_alert_chip()

        self.show_home()

    def _build_home_gauges(self):
        """Bandeau de vu-mètres de santé du Pi en haut de l'accueil."""
        outer = theme.card(self.home_frame)
        outer.pack(fill="x", padx=14, pady=(4, 6))
        row = outer.inner
        row.configure(padding=(10, 8))
        self.home_gauges = {}
        specs = (
            ("temp", "TEMPÉRATURE CPU", "°C", 30, 90, 70, 80),
            ("cpu", "CHARGE CPU", "%", 0, 100, 70, 90),
            ("mem", "MÉMOIRE", "%", 0, 100, 85, 95),
            ("disk", "DISQUE", "%", 0, 100, 85, 95),
        )
        for i, (key, title, unit, lo, hi, warn, crit) in enumerate(specs):
            row.grid_columnconfigure(i, weight=1)
            g = _Gauge(row, title, unit, lo, hi, warn, crit)
            g.grid(row=0, column=i, padx=6)
            self.home_gauges[key] = g
        # Voyant d'alimentation + infos
        power = tk.Frame(row, bg=theme.BG_CARD)
        power.grid(row=0, column=len(specs), padx=(16, 6), sticky="nsew")
        row.grid_columnconfigure(len(specs), weight=1)
        tk.Label(power, text="ALIMENTATION", bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(2, 6))
        led_row = tk.Frame(power, bg=theme.BG_CARD)
        led_row.pack(anchor="w")
        self.home_power_led = tk.Canvas(led_row, width=34, height=34, bg=theme.BG_CARD, highlightthickness=0)
        self.home_power_led.pack(side="left")
        self._home_led_item = self.home_power_led.create_oval(3, 3, 31, 31, fill=theme.MUTED, outline="")
        self.home_power_text = tk.Label(led_row, text="—", bg=theme.BG_CARD, fg=theme.TEXT, font=("Segoe UI", 13, "bold"))
        self.home_power_text.pack(side="left", padx=(10, 0))
        self.home_power_sub = tk.Label(power, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE, justify="left")
        self.home_power_sub.pack(anchor="w", pady=(8, 0))
        self.home_uptime = tk.Label(power, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE, justify="left")
        self.home_uptime.pack(anchor="w", pady=(2, 0))
        # Bouton d'arrêt / relance de la clé SDR, directement sur l'accueil
        key_col = tk.Frame(row, bg=theme.BG_CARD)
        key_col.grid(row=0, column=len(specs) + 1, padx=(10, 6), sticky="ns")
        tk.Label(key_col, text="CLÉ SDR", bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(2, 6))
        self.home_key_label = tk.Label(key_col, text="—", bg=theme.BG_CARD, fg=theme.TEXT, font=("Segoe UI", 13, "bold"))
        self.home_key_label.pack(anchor="w")
        self.home_key_button = tk.Button(
            key_col, text="■  Arrêter la clé SDR", font=theme.FONT_BODY_BOLD, relief="flat", bd=0,
            bg=theme.RED, fg="#0b0f17", activebackground="#ff7b72", activeforeground="#0b0f17",
            padx=14, pady=8, cursor="hand2", command=self._home_key_action,
        )
        self.home_key_button.pack(anchor="w", pady=(10, 0))

        clickable = [outer, row, power, led_row, self.home_power_led, self.home_power_text,
                     self.home_power_sub, self.home_uptime, *self.home_gauges.values()]
        for w in clickable:
            w.bind("<Button-1>", lambda _e: self.show_page("health"))
            try:
                w.configure(cursor="hand2")
            except tk.TclError:
                pass

    def _home_key_action(self):
        if getattr(self, "_sdr_mode", None) == "off":
            self._switch_sdr(getattr(self, "_sdr_last_mode", None) or "adsb")
        else:
            self._stop_sdr()

    def _render_home_key(self):
        if not hasattr(self, "home_key_button"):
            return
        mode = getattr(self, "_sdr_mode", None)
        if mode == "off":
            last = getattr(self, "_sdr_last_mode", None) or "adsb"
            self.home_key_label.config(text="Arrêtée", fg=theme.RED)
            self.home_key_button.config(
                text=f"▶  Relancer ({SDR_MODE_LABELS.get(last, last)})", bg=theme.GREEN,
                activebackground="#56d364",
            )
        else:
            self.home_key_label.config(
                text=SDR_MODE_LABELS.get(mode, "—") if mode else "—", fg=theme.ACCENT if mode else theme.MUTED,
            )
            self.home_key_button.config(text="■  Arrêter la clé SDR", bg=theme.RED, activebackground="#ff7b72")

    def _render_home_gauges(self, data):
        if not hasattr(self, "home_gauges"):
            return
        if data is None:
            for g in self.home_gauges.values():
                g.set(None)
            self.home_power_led.itemconfig(self._home_led_item, fill=theme.MUTED)
            self.home_power_text.config(text="Indisponible", fg=theme.MUTED)
            self.home_power_sub.config(text="")
            self.home_uptime.config(text="")
            return
        load, ncpu = data.get("load"), data.get("cpu_count") or 1
        self.home_gauges["temp"].set(data.get("cpu_temp_c"), "bridage à 80 °C")
        self.home_gauges["cpu"].set(None if not load else min(100, load[0] / ncpu * 100), f"{ncpu} cœurs")
        tot, av = data.get("mem_total"), data.get("mem_available")
        self.home_gauges["mem"].set(None if not tot or av is None else (tot - av) / tot * 100,
                                    f"{self._fmt_bytes(av)} libres" if av is not None else "")
        tot, free = data.get("disk_total"), data.get("disk_free")
        self.home_gauges["disk"].set(None if not tot or free is None else (tot - free) / tot * 100,
                                     f"{self._fmt_bytes(free)} libres" if free is not None else "")
        th = data.get("throttled") or {}
        if th.get("under_voltage_now"):
            col, txt, sub = theme.RED, "SOUS-TENSION", "en ce moment : la clé SDR peut décrocher"
        elif th.get("under_voltage_occurred"):
            col, txt, sub = theme.ORANGE, "À surveiller", "chute de tension depuis le démarrage"
        elif th:
            col, txt, sub = theme.GREEN, "Correcte", "aucune sous-tension"
        else:
            col, txt, sub = theme.MUTED, "Inconnue", ""
        self.home_power_led.itemconfig(self._home_led_item, fill=col)
        self.home_power_text.config(text=txt, fg=col)
        self.home_power_sub.config(text=sub)
        up, n = data.get("uptime_s"), data.get("sdr_dongles")
        parts = []
        if up is not None:
            d, rem = divmod(up, 86400)
            parts.append(f"en marche depuis {d} j {rem // 3600} h" if d else f"en marche depuis {rem // 3600} h {rem % 3600 // 60:02d} min")
        if n is not None:
            parts.append(f"{n} clé(s) SDR sur l'USB")
        self.home_uptime.config(text="  ·  ".join(parts), fg=theme.RED if n == 0 else theme.MUTED)

    def _build_home_tiles(self):
        self._build_home_gauges()
        grid = tk.Frame(self.home_frame, bg=theme.BG)
        grid.pack(fill="both", expand=True, padx=4, pady=4)

        self.tile_chips = {}
        columns = 6
        self.tile_frames = {}
        for c in range(columns):
            grid.grid_columnconfigure(c, weight=1, uniform="tiles")
        for i, (key, title, subtitle) in enumerate(HOME_TILES):
            r, c = divmod(i, columns)
            outer = theme.card(grid)
            outer.grid(row=r, column=c, sticky="nsew", padx=6, pady=6)
            inner = outer.inner
            inner.configure(padding=12)

            header_row = tk.Frame(inner, bg=theme.BG_CARD)
            header_row.pack(anchor="w", fill="x")
            icon_image = _tile_icon(key, size=30)
            icon_label = tk.Label(header_row, image=icon_image, bg=theme.BG_CARD)
            icon_label.image = icon_image  # garde une référence (sinon Tk l'efface du GC)
            icon_label.pack(side="left", padx=(0, 10))
            title_label = tk.Label(
                header_row, text=title, bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION,
            )
            title_label.pack(side="left")
            subtitle_label = tk.Label(
                inner, text=subtitle, bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE,
                wraplength=250, justify="left",
            )
            subtitle_label.pack(anchor="w", pady=(2, 6))
            chip_label = theme.chip(inner, "—")
            chip_label.pack(anchor="w")
            self.tile_chips[key] = _TileChip(self, key, chip_label, outer)
            self.tile_frames[key] = outer

            for widget in (outer, inner, header_row, icon_label, title_label, subtitle_label, chip_label):
                widget.bind("<Button-1>", lambda _e, k=key: self.show_page(k))
                try:
                    widget.configure(cursor="hand2")
                except tk.TclError:
                    pass

    def show_home(self):
        self._show_view(None)

    def show_page(self, key):
        mode = MODULE_SDR_MODE.get(key)
        if key == "beacon":
            mode = self._beacon_page_mode()  # 406, 121,5 ou goniométrie (pas de bascule)
        if key in EXTERNAL_TILES:
            url = EXTERNAL_TILES[key](self.config_data)
            # OpenWebRX met quelques secondes à démarrer : on n'ouvre le
            # navigateur qu'une fois la clé basculée.
            self._switch_sdr(mode, then=lambda: self._open_url(url))
            return
        self._show_view(key)
        self._switch_sdr(mode)

    def _open_url(self, url):
        try:
            webbrowser.open(url)
        except Exception as exc:
            messagebox.showerror("Ouverture", f"Impossible d'ouvrir {url} :\n{exc}")

    def _switch_sdr(self, mode, then=None):
        """Bascule la clé SDR vers `mode` en arrière-plan (sans bloquer
        l'interface). Sans mode, ou si la clé y est déjà, on enchaîne
        directement. Si le service de bascule n'est pas installé sur le
        Pi, on reste silencieux : le reste de l'appli fonctionne."""
        if not mode or mode == getattr(self, "_sdr_mode", None):
            if then:
                then()
            return
        if getattr(self, "_sdr_switching", False):
            self._sdr_pending = (mode, then)  # appliqué dès la fin de la bascule en cours
            return
        self._sdr_switching = True
        label = SDR_MODE_LABELS.get(mode, mode)
        self.chip_sdr.config(text=f"Clé SDR → {label}…")
        host = self.config_data.get("pi_host", "192.168.1.229")
        port = self.config_data.get("port_sdr_switch", 5006)

        def worker():
            try:
                result = api_client.set_sdr_mode(host, port, mode)
                if result.get("ok", True):
                    error = None
                else:
                    failed = ", ".join(result.get("failed") or []) or label
                    error = (f"le service {failed} n'a pas pu démarrer (clé débranchée, ou logiciel absent du Pi ?)"
                             if result.get("failed") else f"aucun service démarré pour {label}")
            except requests.HTTPError as exc:
                try:
                    error = exc.response.json().get("error") or str(exc)
                except Exception:
                    error = str(exc)
                if "aucun service installé" in error:
                    error += " — le logiciel de ce module n'est pas encore installé sur le Pi"
            except Exception as exc:
                error = f"service de bascule du Pi injoignable ({exc})"
            self.after(0, done, error)

        def done(error):
            self._sdr_switching = False
            self._sdr_switch_error = (mode, error, time.time()) if error else None
            if error is None:
                self._sdr_mode = mode
                self.chip_sdr.config(text=f"Clé SDR : {label}")
                self._refresh_all_tiles()
            else:
                self.chip_sdr.config(text="Clé SDR : bascule impossible")
                messagebox.showwarning("Clé SDR", f"Impossible de donner la clé à {label} :\n\n{error}")
            if then:
                # Laisse le temps au service web (OpenWebRX) de démarrer.
                self.after(4000 if error is None else 0, then)
            # Rafraîchit tout de suite pour refléter le nouveau service.
            threading.Thread(target=self._refresh_once, daemon=True).start()
            pending, self._sdr_pending = getattr(self, "_sdr_pending", None), None
            if pending:
                self._switch_sdr(*pending)

        threading.Thread(target=worker, daemon=True).start()

    def _show_view(self, key):
        self.home_frame.pack_forget()
        for page in self.pages.values():
            page.pack_forget()

        if key is None:
            self.home_frame.pack(fill="both", expand=True)
            self.view_title_label.config(text="Accueil")
            self.back_button.pack_forget()
        else:
            self.pages[key].pack(fill="both", expand=True)
            titles = {k: t for k, t, _ in HOME_TILES}
            self.view_title_label.config(text=titles.get(key, key))
            self.back_button.pack(side="left", padx=(0, 12), before=self.view_title_label)

        self._current_view = key
        if key == "s433" and not getattr(self, "_s433_poll_running", False):
            self._s433_poll_running = True
            self._s433_poll()
        if key == "sats":
            self._sat_tick(reschedule=False)
        if key == "aprs" and not getattr(self, "_aprs_poll_running", False):
            self._aprs_poll_running = True
            self._aprs_poll()
        if key == "beacon" and not getattr(self, "_b406_poll_running", False):
            self._b406_poll_running = True
            self._beacon_poll()
        if key == "beacon" and getattr(self, "_beacon_band", "406") in ("121", "gonio"):
            self._elt_start_poll()
        if key == "aircraft" and not getattr(self, "_adsb_poll_running", False):
            self._adsb_poll_running = True
            self._adsb_poll()
        if key == "stats" and not getattr(self, "_stats_poll_running", False):
            self._stats_poll_running = True
            self._stats_poll()
        if key == "alerts" and hasattr(self, "nt_status"):
            self._notify_load()
        if key == "svxlink" and not getattr(self, "_svx_poll_running", False):
            self._svx_poll_running = True
            self._svx_poll()

    def _clear(self, frame):
        for child in frame.winfo_children():
            child.destroy()

    # --------------------------------------------------------- Horloge --

    def _tick_clock(self):
        now = time.localtime()
        self.clock_label.config(text=time.strftime("%H:%M:%S", now))
        self.date_label.config(text=time.strftime("%A %d %B %Y", now))
        self.after(1000, self._tick_clock)

    # ------------------------------------------------------------ Réseau --

    def _start_refresh_loop(self):
        thread = threading.Thread(target=self._refresh_loop, daemon=True)
        thread.start()

    def _refresh_loop(self):
        while not self._stop_event.is_set():
            self._refresh_once()
            self._stop_event.wait(self.config_data.get("refresh_seconds", 15))

    def _refresh_once(self):
        host = self.config_data.get("pi_host", "192.168.1.229")
        cfg = self.config_data

        self._fetch(lambda: api_client.get_weather(host, cfg["port_weather"]), self._render_weather)
        self._fetch(lambda: api_client.get_aircraft(host, cfg["port_adsb"]), self._render_aircraft)
        self._fetch(lambda: api_client.get_services_status(host, cfg["port_services"]), self._render_services)
        self._fetch(
            lambda: api_client.get_system_health(host, cfg.get("port_sdr_switch", 5006)), self._render_health,
        )
        self._fetch(lambda: api_client.get_aprs_stations(host, cfg["port_aprs"]), self._render_aprs_stations)
        self._fetch(lambda: api_client.get_aprs_packets(host, cfg["port_aprs"]), self._render_aprs_packets)
        self._fetch(lambda: api_client.get_aprs_packets(host, cfg["port_aprs"]), self._render_aprs_packets_log)
        self._fetch_lora()
        self._fetch(lambda: api_client.get_ais_ships(host, cfg.get("port_ais", 8100)), self._render_ais)
        self._fetch(
            lambda: api_client.get_sdr_mode(host, cfg.get("port_sdr_switch", 5006)), self._render_sdr_mode,
        )
        self._fetch_winlink()
        self._fetch_vigilance()
        self._fetch_beacon(frames_only=getattr(self, "_current_view", None) != "beacon")
        self._fetch_s433()
        self._elt_fetch_events_bg()
        self._maybe_refresh_weather_overlay()

        self.after(0, lambda: self.chip_updated.config(text=f"Maj : {time.strftime('%H:%M:%S')}"))

    def _fetch(self, fetch_fn, render_fn):
        try:
            data = fetch_fn()
        except requests.RequestException as exc:
            self.after(0, render_fn, None, str(exc))
            return
        except Exception as exc:  # garde-fou : un panneau ne doit jamais planter l'appli
            self.after(0, render_fn, None, f"Erreur inattendue : {exc}")
            return
        self.after(0, render_fn, data, None)

    def _render_error(self, frame, message):
        self._clear(frame)
        tk.Label(
            frame, text=f"Indisponible — {message}", bg=theme.BG, fg=theme.RED,
            font=theme.FONT_BODY, wraplength=800, justify="left",
        ).pack(anchor="w", padx=16, pady=16)

    # --------------------------------------------------------------- Météo --

    def _build_weather_tab(self, frame):
        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        # Colonne de gauche (infos + boutons de calque, largeur naturelle)
        # et colonne de droite (carte), qui occupe tout le reste en largeur
        # ET en hauteur — plutôt qu'empiler les deux verticalement, ce qui
        # laissait la carte étriquée en bas avec un grand vide à droite des
        # infos.
        left_col = tk.Frame(wrapper, bg=theme.BG)
        left_col.pack(side="left", fill="y", padx=(0, 8), pady=12)

        outer = theme.card(left_col)
        outer.pack(fill="x")
        inner = outer.inner
        inner.configure(padding=16)

        self.weather_station_label = tk.Label(
            inner, text="Station : —", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION,
        )
        self.weather_station_label.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))

        row_titles = [
            ("temperature", "Température"), ("vent", "Vent"),
            ("rafales", "Rafales"), ("visibilite", "Visibilité"),
            ("pression", "Pression QNH"), ("point_rosee", "Point de rosée"),
            ("observation", "Observation"), ("source", "Source"),
        ]
        self.weather_value_labels = {}
        for i, (key, title) in enumerate(row_titles):
            r, c = divmod(i, 2)
            cell = tk.Frame(inner, bg=theme.BG_CARD)
            cell.grid(row=r + 1, column=c, sticky="w", padx=(0, 40), pady=4)
            tk.Label(cell, text=title, bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE).pack(anchor="w")
            value_label = tk.Label(cell, text="—", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY_BOLD)
            value_label.pack(anchor="w")
            self.weather_value_labels[key] = value_label

        self.weather_clouds_label = tk.Label(
            inner, text="", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY, justify="left",
        )
        self.weather_clouds_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.weather_metar_label = tk.Label(
            inner, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_MONO,
            wraplength=420, justify="left",
        )
        self.weather_metar_label.grid(row=6, column=0, columnspan=2, sticky="w", pady=(14, 0))

        overlay_row = tk.Frame(left_col, bg=theme.BG)
        overlay_row.pack(fill="x", pady=(8, 0))
        tk.Label(
            overlay_row, text="Carte en direct :", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE,
        ).pack(anchor="w")
        buttons_row = tk.Frame(overlay_row, bg=theme.BG)
        buttons_row.pack(fill="x", pady=(4, 0))
        self._weather_overlay_buttons = {}
        for key, label in (("none", "Aucune"), ("radar", "Radar pluie")):
            btn = ttk.Button(
                buttons_row, text=label, style="Flat.TButton",
                command=lambda k=key: self._set_weather_overlay(k),
            )
            btn.pack(side="left", padx=(0, 4))
            self._weather_overlay_buttons[key] = btn
        self.weather_overlay_label = tk.Label(
            overlay_row, text="", bg=theme.BG, fg=theme.MUTED, font=("Segoe UI", 8), wraplength=240, justify="left",
        )
        self.weather_overlay_label.pack(anchor="w", pady=(4, 0))
        self._build_vigilance_card(left_col)

        map_outer = theme.card(wrapper)
        map_outer.pack(side="left", fill="both", expand=True, pady=12, padx=(0, 12))
        self.weather_map = self._make_map(map_outer.inner)
        self.weather_map.pack(fill="both", expand=True, padx=1, pady=1)
        lat, lon = DEFAULT_COORDS
        self.weather_map.set_position(lat, lon)
        self.weather_map.set_zoom(WEATHER_MAP_DEFAULT_ZOOM)
        self.weather_map.set_marker(lat, lon, text="Station")
        self._weather_map_station = None
        self._weather_overlay_mode = "none"
        self._weather_overlay_last_fetch = 0.0
        self._highlight_weather_overlay_buttons()

    # -- Carte météo en direct (radar pluie / satellite — RainViewer) --

    def _highlight_weather_overlay_buttons(self):
        for key, btn in self._weather_overlay_buttons.items():
            btn.configure(style="Accent.TButton" if key == self._weather_overlay_mode else "Flat.TButton")

    def _set_weather_overlay(self, mode: str):
        self._weather_overlay_mode = mode
        self._highlight_weather_overlay_buttons()
        if mode == "none":
            self.weather_map.set_overlay_tile_server(None)
            self.weather_map.set_zoom(WEATHER_MAP_DEFAULT_ZOOM)
            self._redraw_weather_map_tiles()
            self.weather_overlay_label.config(text="")
            return
        # RainViewer ne documente ses tuiles radar que jusqu'au zoom 7 ; on
        # réduit temporairement le zoom de la carte pour rester dans la
        # couverture garantie (sinon certaines tuiles radar échouent, ce qui
        # fait disparaître aussi le fond de carte sur ces tuiles-là).
        self.weather_map.set_zoom(RAINVIEWER_MAX_ZOOM)
        self.weather_overlay_label.config(text="Chargement...")
        threading.Thread(target=self._fetch_weather_overlay, args=(mode,), daemon=True).start()

    def _fetch_weather_overlay(self, mode: str):
        """Interroge l'API publique et gratuite RainViewer (radar de pluie
        en direct, sans clé requise) et construit l'URL de tuiles
        correspondant à l'image la plus récente."""
        try:
            resp = requests.get(RAINVIEWER_INDEX_URL, timeout=8)
            resp.raise_for_status()
            index = resp.json()
            host = index.get("host", RAINVIEWER_TILE_HOST)
            frames = (index.get("radar") or {}).get("past") or []
            if not frames:
                raise ValueError("Aucune image radar disponible pour l'instant.")
            path = frames[-1]["path"]
            url = f"{host}{path}/256/{{z}}/{{x}}/{{y}}/2/1_1.png"
            label_text = "Radar pluie en direct — RainViewer.com"
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            self.after(0, self._weather_overlay_failed, mode, str(exc))
            return
        self.after(0, self._apply_weather_overlay, mode, url, label_text)

    def _apply_weather_overlay(self, mode: str, url: str, label_text: str):
        if mode != self._weather_overlay_mode:
            return  # l'utilisateur a changé d'avis entre-temps
        self.weather_map.set_overlay_tile_server(url)
        self._redraw_weather_map_tiles()
        self.weather_overlay_label.config(text=label_text)
        self._weather_overlay_last_fetch = time.time()

    def _weather_overlay_failed(self, mode: str, message: str):
        if mode != self._weather_overlay_mode:
            return
        self.weather_overlay_label.config(text=f"Indisponible : {message}")

    def _redraw_weather_map_tiles(self):
        """tkintermapview met en cache les tuiles déjà chargées sans tenir
        compte d'un changement de calque superposé : on force le
        rechargement pour que le radar/satellite apparaisse immédiatement."""
        self.weather_map.tile_image_cache = {}
        self.weather_map.draw_initial_array()

    def _maybe_refresh_weather_overlay(self):
        """Rafraîchit le calque actif toutes les ~5 minutes (le radar
        RainViewer est lui-même mis à jour à peu près à cette fréquence)."""
        if self._weather_overlay_mode == "none":
            return
        if time.time() - self._weather_overlay_last_fetch < 300:
            return
        threading.Thread(
            target=self._fetch_weather_overlay, args=(self._weather_overlay_mode,), daemon=True,
        ).start()

    def _render_weather(self, data, error):
        if error:
            self.weather_station_label.config(text="Station : indisponible")
            for lbl in self.weather_value_labels.values():
                lbl.config(text="—")
            self.weather_clouds_label.config(text="")
            self.weather_metar_label.config(text=f"Indisponible — {error}", fg=theme.RED)
            return

        self._last_weather = data
        self.weather_metar_label.config(fg=theme.MUTED)

        station = data.get("station") or data.get("nom") or "—"
        self.weather_station_label.config(text=f"Station : {station}")

        vent_kt = data.get("vent_vitesse_kt")
        rafales_kt = data.get("vent_rafales_kt")
        values = {
            "temperature": f"{data.get('temperature_c', '—')} °C",
            "vent": f"{data.get('vent_direction_deg', '—')}° — "
                    f"{_fmt(vent_kt)} kt ({_fmt(_kt_to_kmh(vent_kt))} km/h)",
            "rafales": f"{_fmt(rafales_kt)} kt ({_fmt(_kt_to_kmh(rafales_kt))} km/h)",
            "visibilite": f"{data.get('visibilite_sm', '—')} sm",
            "pression": f"{data.get('pression_qnh_hpa', '—')} hPa",
            "point_rosee": f"{data.get('point_rosee_c', '—')} °C",
            "observation": data.get("heure_obs", "—"),
            "source": data.get("source", "—"),
        }
        for key, value in values.items():
            self.weather_value_labels[key].config(text=value)

        nuages = data.get("nuages") or []
        if nuages:
            txt = " · ".join(f"{n.get('cover', '?')} {n.get('base', '?')} ft" for n in nuages)
            self.weather_clouds_label.config(text=f"Nuages : {txt}")
        else:
            self.weather_clouds_label.config(text="")

        self.weather_metar_label.config(text=data.get("metar_brut", ""))

        if "weather" in self.tile_chips:
            temp = data.get("temperature_c")
            self.tile_chips["weather"].config(text=f"{_fmt(temp)} °C" if temp is not None else "—")

        if station != self._weather_map_station:
            lat, lon = STATION_COORDS.get(station, DEFAULT_COORDS)
            self.weather_map.set_position(lat, lon)
            self.weather_map.delete_all_marker()
            self.weather_map.set_marker(lat, lon, text=station)
            self._weather_map_station = station

    # ------------------------------------------------------------- ADS-B --

    def _build_aircraft_table(self, frame):
        outer_wrapper = tk.Frame(frame, bg=theme.BG)
        outer_wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        # Colonne de gauche (liste + fiche détaillée) et carte à droite,
        # comme tar1090.
        wrapper = tk.Frame(outer_wrapper, bg=theme.BG, width=780)
        wrapper.pack(side="left", fill="y", pady=(0, 6))
        wrapper.pack_propagate(False)

        top_row = tk.Frame(wrapper, bg=theme.BG)
        top_row.pack(fill="x", padx=12, pady=(8, 4))
        self.aircraft_count_label = tk.Label(
            top_row, text="Avions : —", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION,
        )
        self.aircraft_count_label.pack(side="left")
        self.aircraft_stats_label = tk.Label(top_row, text="", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.aircraft_stats_label.pack(side="left", padx=(12, 0))
        ttk.Button(
            top_row, text="Carte web tar1090", style="Flat.TButton",
            command=lambda: self._open_url(
                f"http://{self.config_data.get('pi_host', '192.168.1.229')}:{self.config_data.get('port_adsb', 8504)}/"
            ),
        ).pack(side="right")

        search_row = tk.Frame(wrapper, bg=theme.BG)
        search_row.pack(fill="x", padx=12, pady=(0, 4))
        tk.Label(search_row, text="Rechercher (indicatif, hex, immat., type) :", bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_SUBTITLE).pack(side="left", padx=(0, 6))
        self.aircraft_search_var = tk.StringVar()
        self.aircraft_search_var.trace_add("write", lambda *_a: self._repaint_aircraft())
        ttk.Entry(search_row, textvariable=self.aircraft_search_var, width=18).pack(side="left")
        legend = tk.Frame(search_row, bg=theme.BG)
        legend.pack(side="right")
        for txt, col in (("ADS-B", "#4aa3ff"), ("MLAT", "#e3b341"), ("TIS-B", "#f47067"), ("Mode S", "#8b96a5")):
            tk.Label(legend, text=f" {txt} ", bg=theme.BG_CARD_ALT, fg=col, font=("Segoe UI", 8, "bold")).pack(side="left", padx=2)

        table_outer = theme.card(wrapper)
        table_outer.pack(fill="x", padx=12, pady=(0, 6))
        columns = ("flight", "route", "type", "squawk", "alt", "gs", "dist", "rssi")
        self.aircraft_tree = ttk.Treeview(table_outer.inner, columns=columns, show="tree headings", height=6)
        self.aircraft_tree.column("#0", width=36, minwidth=36, stretch=False, anchor="center")
        self.aircraft_tree.heading("#0", text="", command=lambda: self._aircraft_sort_by("country"))
        headings = {
            "flight": ("Indicatif", 92, "w"), "route": ("Route", 84, "center"), "type": ("Type", 58, "center"),
            "squawk": ("Squawk", 62, "center"), "alt": ("Alt. (ft)", 82, "e"), "gs": ("Vit. (kt)", 66, "e"),
            "dist": ("Dist. (NM)", 74, "e"), "rssi": ("RSSI", 56, "e"),
        }
        for col in columns:
            title, width, anchor = headings[col]
            self.aircraft_tree.heading(col, text=title, command=lambda c=col: self._aircraft_sort_by(c))
            self.aircraft_tree.column(col, width=width, minwidth=40, anchor=anchor)
        self.aircraft_tree.tag_configure("mlat", foreground="#e3b341")
        self.aircraft_tree.tag_configure("tisb", foreground="#f47067")
        self.aircraft_tree.tag_configure("modes", foreground="#8b96a5")
        self.aircraft_tree.tag_configure("emerg", background="#5a1f22", foreground="#ffffff")
        vsb = ttk.Scrollbar(table_outer.inner, orient="vertical", command=self.aircraft_tree.yview)
        self.aircraft_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.aircraft_tree.pack(fill="both", expand=True, padx=1, pady=1)
        self.aircraft_tree.bind("<<TreeviewSelect>>", self._on_aircraft_tree_select)
        self._aircraft_sort = ("dist", False)

        # --- Fiche détaillée façon tar1090
        detail_outer = theme.card(wrapper)
        detail_outer.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        d = detail_outer.inner
        d.configure(padding=8)

        head = tk.Frame(d, bg=theme.BG_CARD)
        head.pack(fill="x")
        id_col = tk.Frame(head, bg=theme.BG_CARD)
        id_col.pack(side="left", fill="both", expand=True)
        self.aircraft_detail_title = tk.Label(
            id_col, text="Cliquez un avion (carte ou tableau).", bg=theme.BG_CARD, fg=theme.TEXT,
            font=("Segoe UI", 16, "bold"), anchor="w",
        )
        self.aircraft_detail_title.pack(anchor="w")
        flag_row = tk.Frame(id_col, bg=theme.BG_CARD)
        flag_row.pack(anchor="w")
        self.aircraft_detail_flag = tk.Label(flag_row, bg=theme.BG_CARD)
        self.aircraft_detail_flag.pack(side="left")
        self.aircraft_detail_hex = tk.Label(flag_row, text="", bg=theme.BG_CARD, fg=theme.MUTED,
                                            font=("Consolas", 10), anchor="w")
        self.aircraft_detail_hex.pack(side="left", padx=(6, 0))
        self.aircraft_detail_airline = tk.Label(id_col, text="", bg=theme.BG_CARD, fg=theme.TEXT,
                                                font=theme.FONT_BODY_BOLD, anchor="w", justify="left", wraplength=720)
        self.aircraft_detail_airline.pack(anchor="w", pady=(2, 0))
        self.aircraft_detail_route = tk.Label(id_col, text="", bg=theme.BG_CARD, fg=theme.ACCENT,
                                              font=theme.FONT_BODY_BOLD, anchor="w", justify="left", wraplength=720)
        self.aircraft_detail_route.pack(anchor="w", pady=(2, 0))
        self.aircraft_detail_enriched = tk.Label(id_col, text="", bg=theme.BG_CARD, fg=theme.MUTED,
                                                 font=theme.FONT_SUBTITLE, anchor="w", justify="left", wraplength=720)
        self.aircraft_detail_enriched.pack(anchor="w", pady=(2, 0))

        grid = tk.Frame(d, bg=theme.BG_CARD)
        grid.pack(fill="both", expand=True, pady=(4, 0))
        sections = [
            ("IDENTIFICATION", [("reg", "Immatriculation"), ("country", "Pays"), ("typecode", "Type"),
                                ("typedesc", "Désignation"), ("category", "Catégorie"), ("dbflags", "Indicateurs")]),
            ("SPATIAL", [("gs", "Vitesse sol"), ("alt_baro", "Altitude baro."), ("alt_geom", "Altitude WGS84"),
                         ("vrate", "Vitesse verticale"), ("track", "Route"), ("position", "Position"),
                         ("distance", "Distance / relèvement")]),
            ("SIGNAL", [("source", "Source"), ("rssi", "RSSI"), ("msgrate", "Débit"), ("messages", "Messages"),
                        ("lastpos", "Dernière position"), ("seen", "Dernière réception"), ("squawk", "Squawk")]),
            ("FMS SEL", [("sel_alt", "Altitude sélectionnée"), ("sel_head", "Cap sélectionné"),
                         ("qnh", "QNH"), ("modes", "Modes")]),
            ("DONNÉES AIR", [("ias", "Vitesse indiquée"), ("tas", "Vitesse propre"), ("mach", "Mach"),
                             ("wind", "Vent"), ("temp", "OAT / TAT"), ("roll", "Inclinaison")]),
        ]
        self.aircraft_detail_value_labels = {}
        placement = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1)]
        # La photo occupe la dernière case (ligne 2, colonne 3).
        for (title, fields), (r, c) in zip(sections, placement):
            grid.grid_columnconfigure(c, weight=1, uniform="ad")
            box = tk.Frame(grid, bg=theme.BG_CARD)
            box.grid(row=r, column=c, sticky="nsew", padx=3, pady=2)
            tk.Label(box, text=title, bg="#1f4e5f", fg="#ffffff", font=("Segoe UI", 8, "bold"),
                     anchor="center").pack(fill="x")
            body = tk.Frame(box, bg=theme.BG_CARD)
            body.pack(fill="x", pady=(2, 0))
            body.grid_columnconfigure(1, weight=1)
            for i, (key, label) in enumerate(fields):
                tk.Label(body, text=label + " :", bg=theme.BG_CARD, fg=theme.MUTED, pady=0,
                         font=("Segoe UI", 8), anchor="w").grid(row=i, column=0, sticky="w")
                v = tk.Label(body, text="—", bg=theme.BG_CARD, fg=theme.TEXT, font=("Segoe UI", 8, "bold"),
                             anchor="e", pady=0)
                v.grid(row=i, column=1, sticky="e")
                self.aircraft_detail_value_labels[key] = v
        photo_box = tk.Frame(grid, bg=theme.BG_CARD)
        photo_box.grid(row=1, column=2, sticky="nsew", padx=3, pady=2)
        self.aircraft_photo_label = tk.Label(
            photo_box, bg=theme.BG_CARD_ALT, fg=theme.MUTED, text="—", font=theme.FONT_SUBTITLE, width=24, height=6,
        )
        self.aircraft_photo_label.pack(anchor="center", pady=(2, 0))
        self.aircraft_detail_credit = tk.Label(photo_box, text="", bg=theme.BG_CARD, fg=theme.MUTED,
                                               font=("Segoe UI", 7), anchor="center")
        self.aircraft_detail_credit.pack(anchor="center")

        # --- Carte + légende d'altitude
        right = tk.Frame(outer_wrapper, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True, pady=(8, 12), padx=(0, 12))
        map_outer = theme.card(right)
        map_outer.pack(fill="both", expand=True)
        self.aircraft_map = self._make_map(map_outer.inner)
        self.aircraft_map.pack(fill="both", expand=True, padx=1, pady=1)
        lat, lon = DEFAULT_COORDS
        self.aircraft_map.set_position(lat, lon)
        self.aircraft_map.set_zoom(7)
        self._draw_range_rings()
        self.aircraft_legend = tk.Canvas(right, height=36, bg=theme.BG, highlightthickness=0)
        self.aircraft_legend.pack(fill="x", pady=(4, 0))
        self.aircraft_legend.bind("<Configure>", lambda _e: self._draw_alt_legend())

        self._aircraft_icon_refs = []
        self._aircraft_trails = {}
        self._aircraft_info_cache = {}
        self._selected_aircraft_hex = None
        self._current_photo_image = None
        self._last_aircraft_full = []
        self._last_aircraft_by_hex = {}
        self._ac_msg_hist = {}
        self._ac_msg_rate = {}
        self._adsb_total_prev = None
        self._adsb_msg_rate = None
        self._adsb_poll_running = False
        self.tardb = TarDb(lambda: f"http://{self.config_data.get('pi_host', '192.168.1.229')}:"
                                   f"{self.config_data.get('port_adsb', 8504)}")
        self.routes = RouteCache()

    # -- Carte : cercles de distance et légende

    def _draw_range_rings(self):
        lat0, lon0 = DEFAULT_COORDS
        for nm in (50, 100, 150):
            r_lat = nm / 60.0
            r_lon = r_lat / math.cos(math.radians(lat0))
            pts = [(lat0 + r_lat * math.sin(math.radians(a)), lon0 + r_lon * math.cos(math.radians(a)))
                   for a in range(0, 361, 5)]
            self.aircraft_map.set_polygon(pts, outline_color="#222222", fill_color=None, border_width=1)
            self.aircraft_map.set_marker(lat0 - r_lat, lon0, text=f"{nm} NM", text_color="#333333",
                                         icon=self._blank_icon(), icon_anchor="center")
        self._station_icon = self._station_marker_icon()
        self.aircraft_map.set_marker(lat0, lon0, icon=self._station_icon, icon_anchor="center")

    def _blank_icon(self):
        if not hasattr(self, "_blank_photo"):
            self._blank_photo = ImageTk.PhotoImage(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
        return self._blank_photo

    def _station_marker_icon(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        dr.ellipse([16, 16, 48, 48], fill="#0b0f17")
        dr.ellipse([22, 22, 42, 42], fill="#ffffff")
        return ImageTk.PhotoImage(img.resize((16, 16), Image.LANCZOS))

    def _draw_alt_legend(self):
        c = self.aircraft_legend
        c.delete("all")
        w = max(c.winfo_width(), 300)
        left, right = 90, 20
        span = w - left - right
        c.create_text(4, 12, text="ALTITUDE (ft)", anchor="w", fill=theme.MUTED, font=("Segoe UI", 8, "bold"))
        marks = [0, 500, 1000, 2000, 4000, 6000, 8000, 10000, 20000, 30000, 40000]

        def x_of(alt):  # échelle non linéaire comme tar1090 (détail en basse altitude)
            pos = _interp([(m, i / (len(marks) - 1)) for i, m in enumerate(marks)], alt)
            return left + pos * span

        steps = 120
        for i in range(steps):
            a0 = _interp([(0, 0), (1, 40000)], i / steps)
            a1 = _interp([(0, 0), (1, 40000)], (i + 1) / steps)
            c.create_rectangle(x_of(a0), 4, x_of(a1) + 1, 18, fill=_alt_color(a0), outline="")
        for m in marks:
            x = x_of(m)
            c.create_line(x, 4, x, 21, fill=theme.BG)
            c.create_text(x, 29, text=(f"{m // 1000} 000" if m >= 1000 else str(m)) + ("+" if m == 40000 else ""),
                          fill=theme.MUTED, font=("Segoe UI", 7))

    # -- Tri, sélection

    def _aircraft_sort_by(self, col):
        cur, desc = self._aircraft_sort
        self._aircraft_sort = (col, not desc if cur == col else False)
        self._repaint_aircraft()

    def _on_aircraft_tree_select(self, _event):
        selection = self.aircraft_tree.selection()
        if not selection or selection[0] == self._selected_aircraft_hex:
            return
        ac = self._last_aircraft_by_hex.get(selection[0])
        if ac:
            self._show_aircraft_detail_data(ac)
            if ac.get("lat") is not None and ac.get("lon") is not None:
                self.aircraft_map.set_position(ac["lat"], ac["lon"])
            self._repaint_aircraft(keep_selection=True)

    def _on_aircraft_marker_click(self, marker):
        ac = marker.data
        if isinstance(ac, dict):
            self._show_aircraft_detail_data(ac)
            self._repaint_aircraft()

    # -- Fiche

    @staticmethod
    def _ac_alt(ac):
        alt = ac.get("alt_baro")
        return 0 if alt == "ground" else alt if isinstance(alt, (int, float)) else None

    def _ac_route_text(self, ac, short=True):
        route = self.routes.get(ac.get("flight"))
        if not route:
            return ""
        aps = route["airports"]
        if short:
            return "-".join((a.get("iata") or a.get("icao") or "?") for a in aps)
        return "  →  ".join(f"{a.get('icao') or ''} {a.get('city') or a.get('name') or ''}".strip() for a in aps) + \
            ("" if route.get("plausible", True) else "  (à confirmer)")

    def _show_aircraft_detail_data(self, ac):
        hx = ac.get("hex")
        self._selected_aircraft_hex = hx
        self._refresh_aircraft_detail()
        cached = self._aircraft_info_cache.get(hx) if hx else None
        if cached and cached != "loading":
            self._apply_enriched_info(hx, cached)
        elif not cached:
            self.aircraft_detail_credit.config(text="")
            self._current_photo_image = None
            self.aircraft_photo_label.config(image="", text="Recherche de la photo…")
            if hx:
                self._aircraft_info_cache[hx] = "loading"
                threading.Thread(target=self._fetch_aircraft_info, args=(hx,), daemon=True).start()

    def _refresh_aircraft_detail(self):
        hx = self._selected_aircraft_hex
        ac = self._last_aircraft_by_hex.get(hx) if hx else None
        if not ac:
            return
        db = self.tardb.get(hx)
        flight = (ac.get("flight") or "").strip()
        reg = ac.get("r") or db.get("r")
        typ = ac.get("t") or db.get("t")
        country, ccode = icao_country(hx)
        self.aircraft_detail_title.config(text=flight or reg or hx.upper())
        flag = _flag_image(ccode)
        self.aircraft_detail_flag.config(image=flag or "")
        self.aircraft_detail_flag.image = flag
        self.aircraft_detail_hex.config(text=f"Hex {hx.upper()}" + (f"  ·  {country}" if country else ""))
        airline = self.tardb.airline(flight, reg)
        hexinfo = self._aircraft_info_cache.get(hx) if isinstance(self._aircraft_info_cache.get(hx), dict) else {}
        self.aircraft_detail_airline.config(
            text=(airline or {}).get("n") or (hexinfo or {}).get("operator") or "",
        )
        self.aircraft_detail_route.config(text=self._ac_route_text(ac, short=False))

        num = lambda k: ac.get(k) if isinstance(ac.get(k), (int, float)) else None
        lat, lon = num("lat"), num("lon")
        slat, slon = DEFAULT_COORDS
        dist = _haversine_nm(slat, slon, lat, lon) if lat is not None and lon is not None else None
        brg = _bearing_deg(slat, slon, lat, lon) if dist is not None else None
        vr = num("baro_rate") if num("baro_rate") is not None else num("geom_rate")
        alt = ac.get("alt_baro")
        rate = self._ac_msg_rate.get(hx)
        wd, ws = num("wd"), num("ws")
        oat, tat = num("oat"), num("tat")
        modes = ac.get("nav_modes") or []
        mode_names = {"autopilot": "AP", "vnav": "VNAV", "althold": "ALT", "approach": "APP", "lnav": "LNAV",
                      "tcas": "TCAS"}
        cat = ac.get("category")
        cat_names = {"A1": "léger", "A2": "petit", "A3": "gros", "A4": "B757", "A5": "lourd", "A6": "très performant",
                     "A7": "hélicoptère", "B1": "planeur", "B2": "ballon", "B4": "ULM", "B6": "drone",
                     "C1": "véhicule d'urgence", "C2": "véhicule de service", "C3": "obstacle"}
        values = {
            "reg": reg or "—",
            "country": country or "—",
            "typecode": " · ".join(x for x in (typ, db.get("long") or ac.get("desc")) if x) or "—",
            "typedesc": " · ".join(x for x in (db.get("desc"), f"turbulence {db['wtc']}" if db.get("wtc") else None) if x) or "—",
            "category": f"{cat} ({cat_names.get(cat, '?')})" if cat else "—",
            "dbflags": ", ".join(db.get("flags") or []) or ("aucun" if db else "—"),
            "gs": f"{num('gs'):.0f} kt ({num('gs') * 1.852:.0f} km/h)" if num("gs") is not None else "—",
            "alt_baro": "au sol" if alt == "ground" else (f"{alt:,.0f} ft".replace(",", " ") if num("alt_baro") is not None else "—"),
            "alt_geom": f"{num('alt_geom'):,.0f} ft".replace(",", " ") if num("alt_geom") is not None else "—",
            "vrate": f"{vr:+.0f} ft/min" if vr is not None else "—",
            "track": f"{num('track'):.1f}°" if num("track") is not None else "—",
            "position": f"{lat:.3f}°, {lon:.3f}°" if lat is not None else "—",
            "distance": f"{dist:.1f} NM / {brg:.0f}°" if dist is not None else "—",
            "source": _source_label(ac.get("type")),
            "rssi": f"{num('rssi'):.1f} dBFS" if num("rssi") is not None else "—",
            "msgrate": f"{rate:.1f} msg/s" if rate is not None else "—",
            "messages": f"{ac.get('messages', 0):,}".replace(",", " ") if ac.get("messages") is not None else "—",
            "lastpos": f"{num('seen_pos'):.1f} s" if num("seen_pos") is not None else "—",
            "seen": f"{num('seen'):.1f} s" if num("seen") is not None else "—",
            "squawk": str(ac.get("squawk") or "—") + (f"  ({self.SQUAWK_ALERTS[ac['squawk']]})" if ac.get("squawk") in self.SQUAWK_ALERTS else ""),
            "sel_alt": f"{num('nav_altitude_mcp'):,.0f} ft".replace(",", " ") if num("nav_altitude_mcp") is not None else
                       (f"{num('nav_altitude_fms'):,.0f} ft (FMS)".replace(",", " ") if num("nav_altitude_fms") is not None else "—"),
            "sel_head": f"{num('nav_heading'):.1f}°" if num("nav_heading") is not None else "—",
            "qnh": f"{num('nav_qnh'):.1f} hPa" if num("nav_qnh") is not None else "—",
            "modes": " ".join(mode_names.get(m, m) for m in modes) or "—",
            "ias": f"{num('ias'):.0f} kt" if num("ias") is not None else "—",
            "tas": f"{num('tas'):.0f} kt" if num("tas") is not None else "—",
            "mach": f"{num('mach'):.3f}" if num("mach") is not None else "—",
            "wind": f"{wd:.0f}° / {ws:.0f} kt" if wd is not None and ws is not None else "—",
            "temp": " / ".join(f"{t:.0f} °C" for t in (oat, tat) if t is not None) or "—",
            "roll": f"{num('roll'):.1f}°" if num("roll") is not None else "—",
        }
        for key, label in self.aircraft_detail_value_labels.items():
            v = values.get(key, "—")
            label.config(text=v, fg=theme.RED if key == "squawk" and ac.get("squawk") in self.SQUAWK_ALERTS else theme.TEXT)

    def _fetch_aircraft_info(self, hx):
        """Photo (planespotters.net) et, en secours si la base tar1090 du Pi
        ne connaît pas l'avion, type/compagnie via hexdb.io."""
        info = {"type": None, "operator": None, "registration": None,
                "photo_url": None, "photographer": None, "_photo_image": None}
        if not self.tardb.get(hx):
            try:
                resp = requests.get(f"https://hexdb.io/api/v1/aircraft/{hx}", timeout=6)
                if resp.ok:
                    data = resp.json()
                    if isinstance(data, dict):
                        info["type"] = data.get("ModelFullName") or data.get("Type") or data.get("ICAOTypeCode")
                        info["operator"] = data.get("RegisteredOwners") or data.get("OperatorFlagCode")
                        info["registration"] = data.get("Registration")
            except (requests.RequestException, ValueError):
                pass
        photo_url = None
        try:
            resp = requests.get(f"https://api.planespotters.net/pub/photos/hex/{hx}", timeout=6)
            if resp.ok:
                photos = resp.json().get("photos") or []
                if photos:
                    p = photos[0]
                    thumb = p.get("thumbnail_large") or p.get("thumbnail") or {}
                    photo_url = thumb.get("src")
                    info["photographer"] = p.get("photographer")
        except (requests.RequestException, ValueError):
            pass
        if photo_url:
            try:
                img_resp = requests.get(photo_url, timeout=8)
                img_resp.raise_for_status()
                img = Image.open(io.BytesIO(img_resp.content)).convert("RGB")
                img.thumbnail((180, 120))
                info["_photo"] = img
            except Exception:
                info["_photo"] = None
        self.after(0, self._finish_aircraft_info, hx, info)

    def _finish_aircraft_info(self, hx, info):
        if info.get("_photo") is not None:
            info["_photo_image"] = ImageTk.PhotoImage(info.pop("_photo"))
        self._aircraft_info_cache[hx] = info
        self._apply_enriched_info(hx, info)

    def _apply_enriched_info(self, hx, info):
        if hx != self._selected_aircraft_hex:
            return
        extra = [x for x in (info.get("registration"), info.get("type")) if x and not self.tardb.get(hx)]
        self.aircraft_detail_enriched.config(text=" · ".join(extra) + ("  (hexdb.io)" if extra else ""))
        photo_image = info.get("_photo_image")
        if photo_image is not None:
            self._current_photo_image = photo_image
            self.aircraft_photo_label.config(image=photo_image, text="", width=180, height=120)
            credit = info.get("photographer")
            self.aircraft_detail_credit.config(
                text=f"Photo © {credit} — planespotters.net" if credit else "Photo : planespotters.net")
        else:
            self._current_photo_image = None
            self.aircraft_photo_label.config(image="", text="Pas de photo", width=24, height=6)
            self.aircraft_detail_credit.config(text="")
        self._refresh_aircraft_detail()

    # -- Données

    def _adsb_poll(self):
        """Rafraîchissement rapide (3 s) tant que la page est affichée."""
        if getattr(self, "_current_view", None) != "aircraft":
            self._adsb_poll_running = False
            return
        host = self.config_data.get("pi_host", "192.168.1.229")
        port = self.config_data.get("port_adsb", 8504)
        threading.Thread(target=self._fetch, args=(lambda: api_client.get_aircraft(host, port), self._render_aircraft),
                         daemon=True).start()
        self.after(3000, self._adsb_poll)

    def _render_aircraft(self, data, error):
        if error:
            self.aircraft_count_label.config(text="Avions : indisponible")
            self.aircraft_stats_label.config(text="")
            self._last_aircraft_full = []
            self._last_aircraft_by_hex = {}
            self._repaint_aircraft()
            if "aircraft" in self.tile_chips:
                self.tile_chips["aircraft"].config(text="—")
            self.chip_aircraft.config(text="Avions : —")
            return

        now = data.get("now") or time.time()
        aircraft = [ac for ac in data.get("aircraft", []) if (ac.get("seen") or 0) < 60]
        self._last_aircraft_full = aircraft
        self._last_aircraft_by_hex = {ac["hex"]: ac for ac in aircraft if ac.get("hex")}

        # Débits de messages (global et par avion), comme tar1090.
        total = data.get("messages")
        if isinstance(total, (int, float)):
            prev = self._adsb_total_prev
            if prev and now > prev[1] and total >= prev[0]:
                self._adsb_msg_rate = (total - prev[0]) / (now - prev[1])
            self._adsb_total_prev = (total, now)
        hist = {}
        for ac in aircraft:
            hx, msgs = ac.get("hex"), ac.get("messages")
            if hx and isinstance(msgs, int):
                old = self._ac_msg_hist.get(hx)
                if old and now > old[1] and msgs >= old[0]:
                    self._ac_msg_rate[hx] = (msgs - old[0]) / (now - old[1])
                hist[hx] = (msgs, now)
        self._ac_msg_hist = hist

        new_trails = {}
        for ac in aircraft:
            hx, lat, lon = ac.get("hex"), ac.get("lat"), ac.get("lon")
            if hx and lat is not None and lon is not None:
                trail = self._aircraft_trails.get(hx) or deque(maxlen=60)
                if not trail or trail[-1][:2] != (lat, lon):
                    trail.append((lat, lon, ac.get("alt_baro")))
                new_trails[hx] = trail
        self._aircraft_trails = new_trails

        self.tardb.request([ac.get("hex") for ac in aircraft])
        self.routes.request([(ac.get("flight"), ac.get("lat"), ac.get("lon")) for ac in aircraft])

        with_pos = sum(1 for ac in aircraft if ac.get("lat") is not None)
        self.aircraft_count_label.config(text=f"Avions : {len(aircraft)}")
        rate = f"  ·  Messages : {self._adsb_msg_rate:.1f}/s" if self._adsb_msg_rate is not None else ""
        self.aircraft_stats_label.config(text=f"Avec position : {with_pos}{rate}")
        self._check_aircraft_alerts(aircraft)
        if "aircraft" in self.tile_chips:
            self.tile_chips["aircraft"].config(text=f"{len(aircraft)} avion(s)")
        self.chip_aircraft.config(text=f"Avions : {len(aircraft)}")

        self._repaint_aircraft(keep_selection=True)
        self._refresh_aircraft_detail()

    def _repaint_aircraft(self, keep_selection=True):
        if not hasattr(self, "aircraft_tree"):
            return
        query = self.aircraft_search_var.get().strip().lower()
        aircraft = self._last_aircraft_full
        if query:
            def match(ac):
                db = self.tardb.get(ac.get("hex"))
                hay = " ".join(str(x or "") for x in (ac.get("flight"), ac.get("hex"), ac.get("r") or db.get("r"),
                                                      ac.get("t") or db.get("t"))).lower()
                return query in hay
            aircraft = [ac for ac in aircraft if match(ac)]

        slat, slon = DEFAULT_COORDS

        def dist_of(ac):
            if ac.get("lat") is None or ac.get("lon") is None:
                return None
            return _haversine_nm(slat, slon, ac["lat"], ac["lon"])

        col, desc = self._aircraft_sort

        def key(ac):
            db = self.tardb.get(ac.get("hex"))
            vals = {
                "flight": (ac.get("flight") or "").strip() or "~",
                "route": self._ac_route_text(ac) or "~",
                "type": ac.get("t") or db.get("t") or "~",
                "squawk": str(ac.get("squawk") or "~"),
                "alt": self._ac_alt(ac),
                "gs": ac.get("gs"),
                "dist": dist_of(ac),
                "rssi": ac.get("rssi"),
                "country": icao_country(ac.get("hex"))[1] or "~",
            }
            v = vals.get(col)
            if isinstance(v, str):
                return (0, v)
            return (0 if v is not None else 1, v if v is not None else 0)
        aircraft = sorted(aircraft, key=key, reverse=desc)
        if col not in ("flight", "route", "type", "squawk", "country") and desc:
            aircraft = [a for a in aircraft if key(a)[0] == 0] + [a for a in aircraft if key(a)[0] == 1]

        tree = self.aircraft_tree
        tree.delete(*tree.get_children())
        for ac in aircraft:
            hx = ac.get("hex")
            if not hx or tree.exists(hx):
                continue
            db = self.tardb.get(hx)
            alt = ac.get("alt_baro")
            vr = ac.get("baro_rate") if ac.get("baro_rate") is not None else ac.get("geom_rate")
            arrow = "▲ " if isinstance(vr, (int, float)) and vr > 256 else "▼ " if isinstance(vr, (int, float)) and vr < -256 else ""
            alt_txt = "sol" if alt == "ground" else (f"{arrow}{alt:,.0f}".replace(",", " ") if isinstance(alt, (int, float)) else "")
            d = dist_of(ac)
            src = str(ac.get("type") or "")
            tags = ["mlat"] if src == "mlat" else ["tisb"] if src.startswith("tisb") else ["modes"] if src == "mode_s" else []
            if ac.get("squawk") in self.SQUAWK_ALERTS:
                tags.append("emerg")
            flag = _flag_image(icao_country(hx)[1])
            tree.insert(
                "", "end", iid=hx, image=flag or "", tags=tags,
                values=(
                    (ac.get("flight") or "").strip() or ac.get("r") or db.get("r") or "",
                    self._ac_route_text(ac), ac.get("t") or db.get("t") or "", ac.get("squawk") or "",
                    alt_txt, f"{ac['gs']:.0f}" if isinstance(ac.get("gs"), (int, float)) else "",
                    f"{d:.1f}" if d is not None else "",
                    f"{ac['rssi']:.1f}" if isinstance(ac.get("rssi"), (int, float)) else "",
                ),
            )
        if keep_selection and self._selected_aircraft_hex and tree.exists(self._selected_aircraft_hex):
            tree.selection_set(self._selected_aircraft_hex)

        # Carte
        self.aircraft_map.delete_all_path()
        for m in list(getattr(self, "_aircraft_markers", [])):
            try:
                m.delete()
            except Exception:
                pass
        self._aircraft_markers = []
        self._aircraft_icon_refs = []
        sel = self._selected_aircraft_hex
        for ac in aircraft:
            lat, lon = ac.get("lat"), ac.get("lon")
            if lat is None or lon is None:
                continue
            hx = ac.get("hex")
            is_sel = hx == sel
            trail = self._aircraft_trails.get(hx)
            if trail and len(trail) >= 2:
                pts = list(trail) if is_sel else list(trail)[-12:]
                for p0, p1 in zip(pts, pts[1:]):
                    self.aircraft_map.set_path([p0[:2], p1[:2]], color=_alt_color(p1[2]), width=3 if is_sel else 2)
            icon = _plane_icon(ac.get("alt_baro"), ac.get("track"), selected=is_sel)
            self._aircraft_icon_refs.append(icon)
            label = (ac.get("flight") or "").strip() if is_sel else None
            self._aircraft_markers.append(self.aircraft_map.set_marker(
                lat, lon, text=label, icon=icon, icon_anchor="center",
                data=ac, command=self._on_aircraft_marker_click,
            ))

    # ----------------------------------------------------------- APRS --

    def _build_aprs_tab(self, frame):
        outer = tk.Frame(frame, bg=theme.BG)
        outer.pack(fill="both", expand=True, padx=4, pady=4)
        left = tk.Frame(outer, bg=theme.BG, width=860)
        left.pack(side="left", fill="y", pady=(0, 6))
        left.pack_propagate(False)

        st_outer = theme.card(left)
        st_outer.pack(fill="x", padx=12, pady=(8, 6))
        st = st_outer.inner
        st.configure(padding=12)
        r1 = tk.Frame(st, bg=theme.BG_CARD)
        r1.pack(fill="x")
        tk.Label(r1, text="LoRa 433,775 MHz (iGate F4JCF-10)", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_BODY_BOLD).pack(side="left")
        self.lora_badge = theme.pill(r1, "INCONNU", theme.MUTED)
        self.lora_badge.pack(side="left", padx=(10, 0))
        ttk.Button(r1, text="Page de l'iGate", style="Flat.TButton",
                   command=lambda: self._open_url(f"http://{self.config_data.get('lora_igate_host', '192.168.1.154')}/")
                   ).pack(side="right")
        self.lora_info = tk.Label(st, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE, anchor="w")
        self.lora_info.pack(anchor="w", pady=(2, 8))
        r2 = tk.Frame(st, bg=theme.BG_CARD)
        r2.pack(fill="x")
        tk.Label(r2, text="VHF 144,800 MHz (Direwolf, clé SDR)", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_BODY_BOLD).pack(side="left")
        self.aprs_badge = theme.pill(r2, "INCONNU", theme.MUTED)
        self.aprs_badge.pack(side="left", padx=(10, 0))
        ttk.Button(r2, text="Redémarrer Direwolf", style="Flat.TButton",
                   command=lambda: self._restart_service("direwolf")).pack(side="right")
        ttk.Button(r2, text="Carte aprs.fi (F4JCF-10)", style="Flat.TButton",
                   command=lambda: self._open_url("https://aprs.fi/#!call=a%2FF4JCF-10&timerange=3600&tail=3600")
                   ).pack(side="right", padx=(0, 8))
        self.aprs_vhf_info = tk.Label(st, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE, anchor="w")
        self.aprs_vhf_info.pack(anchor="w", pady=(2, 0))

        self.aprs_count_label = tk.Label(left, text="Stations entendues : —", bg=theme.BG, fg=theme.TEXT,
                                         font=theme.FONT_SECTION)
        self.aprs_count_label.pack(anchor="w", padx=12, pady=(2, 4))
        tbl = theme.card(left)
        tbl.pack(fill="x", padx=12, pady=(0, 6))
        cols = ("src", "call", "heard", "dist", "sig", "count", "sym", "comment")
        self.aprs_tree = ttk.Treeview(tbl.inner, columns=cols, show="headings", height=10)
        spec = {"src": ("Source", 60, "center"), "call": ("Indicatif", 100, "w"), "heard": ("Dernière réc.", 92, "center"),
                "dist": ("Distance / relèvement", 140, "e"), "sig": ("RSSI / SNR", 98, "center"),
                "count": ("Nb", 40, "center"), "sym": ("Symb.", 46, "center"), "comment": ("Commentaire", 230, "w")}
        for c in cols:
            t, w, a = spec[c]
            self.aprs_tree.heading(c, text=t, command=lambda col=c: self._aprs_sort_by(col))
            self.aprs_tree.column(c, width=w, minwidth=36, anchor=a)
        self.aprs_tree.tag_configure("lora", foreground="#60a5fa")
        self.aprs_tree.tag_configure("vhf", foreground="#4ade80")
        self.aprs_tree.tag_configure("iss", foreground="#c084fc")
        vsb = ttk.Scrollbar(tbl.inner, orient="vertical", command=self.aprs_tree.yview)
        self.aprs_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.aprs_tree.pack(fill="x", padx=1, pady=1)
        self.aprs_tree.bind("<<TreeviewSelect>>", self._on_aprs_select)
        self._aprs_sort = ("heard", True)

        tk.Label(left, text="Derniers paquets reçus", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_BODY_BOLD).pack(
            anchor="w", padx=12, pady=(4, 2))
        pk = theme.card(left)
        pk.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self.aprs_packets = tk.Text(pk.inner, bg=theme.BG_CARD_ALT, fg=theme.TEXT, font=("Consolas", 8), relief="flat",
                                    wrap="none", height=8, padx=6, pady=4)
        self.aprs_packets.tag_configure("lora", foreground="#60a5fa")
        self.aprs_packets.tag_configure("vhf", foreground="#4ade80")
        self.aprs_packets.tag_configure("iss", foreground="#c084fc")
        self.aprs_packets.pack(fill="both", expand=True, padx=1, pady=1)
        self.aprs_packets.config(state="disabled")

        right = tk.Frame(outer, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True, pady=(8, 12), padx=(0, 12))
        map_outer = theme.card(right)
        map_outer.pack(fill="both", expand=True)
        self.aprs_map = self._make_map(map_outer.inner)
        self.aprs_map.pack(fill="both", expand=True, padx=1, pady=1)
        self.aprs_map.set_position(*DEFAULT_COORDS)
        self.aprs_map.set_zoom(9)
        legend = tk.Frame(right, bg=theme.BG)
        legend.pack(anchor="w", pady=(4, 0))
        for txt, col in (("● LoRa (iGate)", "#60a5fa"), ("● VHF 144,800", "#4ade80"), ("● via ISS", "#c084fc")):
            tk.Label(legend, text=txt, bg=theme.BG, fg=col, font=theme.FONT_SUBTITLE).pack(side="left", padx=(0, 14))

        self._vhf_stations = []
        self._vhf_packets = []
        self._vhf_pos = {}
        self._vhf_counts = {}
        self._lora_stations = {}
        self._lora_seen = set()
        self._lora_packets = []
        self._lora_ok = None
        self._aprs_selected = None
        self._aprs_markers = []
        self._aprs_poll_running = False

    # -- LoRa (lu directement depuis le PC sur la page web de l'iGate)

    def _fetch_lora(self):
        host = self.config_data.get("lora_igate_host", "192.168.1.154")
        try:
            packets = requests.get(f"http://{host}/received-packets.json", timeout=5).json()
        except Exception as exc:
            self.after(0, self._render_lora, None, None, str(exc))
            return
        stations = None
        try:
            r = requests.get(f"http://{host}/stations.json", timeout=5)
            if r.ok:
                stations = r.json()
        except Exception:
            stations = None
        self.after(0, self._render_lora, packets, stations, None)

    @staticmethod
    def _parse_rx_time(date_s, time_s):
        for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return time.mktime(time.strptime(f"{date_s} {time_s}", fmt))
            except (ValueError, TypeError):
                continue
        return None

    def _render_lora(self, packets, stations, error):
        if not hasattr(self, "lora_badge"):
            return
        if error:
            self._lora_ok = False
            self.lora_badge.config(text="  INJOIGNABLE  ", bg=theme.RED)
            self.lora_info.config(text=(f"Pas de réponse de l'iGate ({self.config_data.get('lora_igate_host', '192.168.1.154')}) "
                                        "depuis ce PC : TTGO éteint, ou PC et iGate pas sur le même réseau."))
            self._repaint_aprs()
            return
        self._lora_ok = True
        now = time.time()
        new = 0
        for p in packets if isinstance(packets, list) else []:
            raw = str(p.get("packet") or "")
            key = (p.get("rxDate"), p.get("rxTime"), raw)
            if key in self._lora_seen:
                continue
            self._lora_seen.add(key)
            new += 1
            t = self._parse_rx_time(p.get("rxDate"), p.get("rxTime")) or now
            if t > now + 120 or t < now - 7 * 86400:
                t = now
            tnc = aprs_parse.parse_tnc2(raw)
            self._lora_packets.append({"time": t, "src": "lora", "raw": re.sub(r"^[.<]+", "", raw),
                                       "rssi": p.get("RSSI"), "snr": p.get("SNR")})
            if not tnc:
                continue
            st = self._lora_stations.setdefault(tnc["src"], {"callsign": tnc["src"], "source": "lora", "count": 0})
            st["count"] += 1
            st["last_heard"] = max(st.get("last_heard", 0), t)
            st["rssi"], st["snr"] = p.get("RSSI"), p.get("SNR")
            st["path"] = ",".join(tnc["path"])
            pos = aprs_parse.parse_position(tnc["dest"], tnc["info"])
            if pos:
                st.update(lat=pos["lat"], lon=pos["lon"], symbol=pos["symbol"], comment=pos["comment"][:80])
            elif not st.get("comment"):
                st["comment"] = tnc["info"][:80]
        # stations.json (firmware récent) : positions déjà décodées par l'iGate
        for s in stations if isinstance(stations, list) else []:
            call = s.get("callsign")
            if not call or s.get("lat") in (None, 0):
                continue
            st = self._lora_stations.setdefault(call, {"callsign": call, "source": "lora", "count": 0,
                                                       "last_heard": now})
            st.update(lat=s.get("lat"), lon=s.get("lon"), symbol=s.get("symbol") or st.get("symbol"))
            st["count"] = max(st["count"], s.get("count") or 0)
            st["rssi"], st["snr"] = s.get("RSSI", st.get("rssi")), s.get("SNR", st.get("snr"))
        self._lora_packets = sorted(self._lora_packets, key=lambda x: x["time"])[-300:]
        if len(self._lora_seen) > 2000:
            self._lora_seen = set(list(self._lora_seen)[-1000:])
        last = max((s.get("last_heard", 0) for s in self._lora_stations.values()), default=0)
        recent = sum(1 for s in self._lora_stations.values() if now - s.get("last_heard", 0) < 3600)
        self.lora_badge.config(text="  EN LIGNE  ", bg=theme.GREEN)
        self.lora_info.config(text=(f"{len(packets or [])} paquet(s) en mémoire dans l'iGate  ·  {recent} station(s) "
                                    f"la dernière heure" + (f"  ·  dernier paquet à {time.strftime('%H:%M:%S', time.localtime(last))}"
                                                            if last else "")))
        self._repaint_aprs()

    def _aprs_poll(self):
        if getattr(self, "_current_view", None) != "aprs":
            self._aprs_poll_running = False
            return
        threading.Thread(target=self._fetch_lora, daemon=True).start()
        self.after(10000, self._aprs_poll)

    # -- VHF (Pi)

    def _render_aprs_stations(self, data, error):
        if not hasattr(self, "aprs_vhf_info"):
            return
        if error:
            mode = getattr(self, "_sdr_mode", None)
            self._vhf_stations = []
            self.aprs_vhf_info.config(text=(f"En pause : la clé SDR est sur {SDR_MODE_LABELS.get(mode, mode)}."
                                            if mode not in (None, "aprs", "iss") else
                                            "API APRS du Pi injoignable (aprs-api.service)."))
        else:
            self._vhf_stations = data or []
            mode = getattr(self, "_sdr_mode", None)
            listening = "écoute en cours" if mode in ("aprs", "iss") else (
                f"en pause (clé sur {SDR_MODE_LABELS.get(mode, mode)})" if mode else "")
            self.aprs_vhf_info.config(text=f"{len(self._vhf_stations)} station(s) entendue(s)  ·  {listening}"
                                           + ("  ·  fréquence ISS 145,825 MHz" if mode == "iss" else ""))
        self._repaint_aprs()

    def _render_aprs_packets_log(self, data, error):
        if error or not isinstance(data, list):
            return
        out = []
        for p in data[-150:]:
            raw = f"{p.get('callsign')}>{p.get('dest')}{',' + ','.join(p['path']) if p.get('path') else ''}:{p.get('raw', '')}"
            via_iss = "RS0ISS" in raw or "ARISS" in raw
            out.append({"time": p.get("last_heard") or 0, "src": "iss" if via_iss else "vhf", "raw": raw})
            # Le Pi ne décode que les positions en clair : on complète ici
            # (formats compressé et Mic-E, fréquents sur les trackers).
            pos = aprs_parse.parse_position(p.get("dest") or "", p.get("raw") or "")
            if pos and p.get("callsign"):
                self._vhf_pos[p["callsign"]] = pos
        counts = {}
        for p in data:
            counts[p.get("callsign")] = counts.get(p.get("callsign"), 0) + 1
        self._vhf_counts = counts
        self._vhf_packets = out
        self._repaint_aprs_packets()

    # -- Fusion et affichage

    def _aprs_all(self):
        rows = []
        for s in self._vhf_stations:
            path = s.get("path") or []
            path_s = ",".join(path) if isinstance(path, list) else str(path)
            src = "iss" if ("RS0ISS" in path_s or "ARISS" in path_s) else "vhf"
            pos = self._vhf_pos.get(s.get("callsign")) or {}
            rows.append({"source": src, "callsign": s.get("callsign"), "last_heard": s.get("last_heard"),
                         "lat": s.get("lat") if s.get("lat") is not None else pos.get("lat"),
                         "lon": s.get("lon") if s.get("lon") is not None else pos.get("lon"),
                         "symbol": s.get("symbol") or pos.get("symbol"),
                         "comment": s.get("comment") or pos.get("comment", ""),
                         "count": self._vhf_counts.get(s.get("callsign")), "rssi": None, "snr": None})
        rows += list(self._lora_stations.values())
        slat, slon = DEFAULT_COORDS
        for r in rows:
            if r.get("lat") is not None and r.get("lon") is not None:
                r["dist_km"] = _haversine_nm(slat, slon, r["lat"], r["lon"]) * 1.852
                r["brg"] = _bearing_deg(slat, slon, r["lat"], r["lon"])
            else:
                r["dist_km"] = r["brg"] = None
        return rows

    def _aprs_sort_by(self, col):
        cur, desc = self._aprs_sort
        self._aprs_sort = (col, not desc if cur == col else col in ("heard", "count", "dist"))
        self._repaint_aprs()

    def _repaint_aprs(self):
        rows = self._aprs_all()
        col, desc = self._aprs_sort
        keyf = {"src": lambda r: r["source"], "call": lambda r: r["callsign"] or "",
                "heard": lambda r: r.get("last_heard") or 0, "dist": lambda r: r["dist_km"] if r["dist_km"] is not None else -1,
                "sig": lambda r: r.get("rssi") if r.get("rssi") is not None else -999, "count": lambda r: r.get("count") or 0,
                "sym": lambda r: r.get("symbol") or "", "comment": lambda r: r.get("comment") or ""}[col]
        rows.sort(key=keyf, reverse=desc)
        lora = sum(1 for r in rows if r["source"] == "lora")
        self.aprs_count_label.config(text=f"Stations entendues : {len(rows)}  (LoRa {lora} · VHF {len(rows) - lora})")
        if "aprs" in self.tile_chips:
            self.tile_chips["aprs"].config(text=f"{len(rows)} station(s) · LoRa {lora}")
        tree = self.aprs_tree
        tree.delete(*tree.get_children())
        names = {"lora": "LoRa", "vhf": "VHF", "iss": "ISS"}
        for i, r in enumerate(rows):
            lh = r.get("last_heard")
            dist = (f"{r['dist_km']:.1f} km · {r['brg']:.0f}° {self._compass(r['brg'])}" if r["dist_km"] is not None else "—")
            sig = (f"{r['rssi']} dBm / {r['snr']} dB" if r.get("rssi") is not None else "")
            iid = f"{r['source']}:{r['callsign']}"
            if tree.exists(iid):
                continue
            tree.insert("", "end", iid=iid, tags=(r["source"],), values=(
                names[r["source"]], r["callsign"], time.strftime("%H:%M:%S", time.localtime(lh)) if lh else "—",
                dist, sig, r.get("count") or "", r.get("symbol") or "", r.get("comment") or ""))
        if self._aprs_selected and tree.exists(self._aprs_selected):
            tree.selection_set(self._aprs_selected)
        # carte
        for m in self._aprs_markers:
            try:
                m.delete()
            except Exception:
                pass
        self._aprs_markers = [self.aprs_map.set_marker(*DEFAULT_COORDS, text="F4JCF", text_color="#16a34a",
                                                       marker_color_circle="#14532d", marker_color_outside="#22c55e")]
        colors = {"lora": ("#1e3a8a", "#60a5fa", "#1d4ed8"), "vhf": ("#14532d", "#4ade80", "#15803d"),
                  "iss": ("#581c87", "#c084fc", "#7e22ce")}
        for r in rows:
            if r["dist_km"] is None:
                continue
            c_in, c_out, c_txt = colors[r["source"]]
            self._aprs_markers.append(self.aprs_map.set_marker(
                r["lat"], r["lon"], text=r["callsign"], text_color=c_txt,
                marker_color_circle=c_in, marker_color_outside=c_out))
        self._repaint_aprs_packets()

    def _repaint_aprs_packets(self):
        if not hasattr(self, "aprs_packets"):
            return
        items = sorted(self._vhf_packets + self._lora_packets, key=lambda p: p["time"])[-120:]
        txt = self.aprs_packets
        txt.config(state="normal")
        txt.delete("1.0", "end")
        names = {"lora": "LoRa", "vhf": "VHF ", "iss": "ISS "}
        for p in reversed(items):
            extra = f"  [{p['rssi']} dBm]" if p.get("rssi") is not None else ""
            txt.insert("end", f"{time.strftime('%H:%M:%S', time.localtime(p['time']))} {names[p['src']]}  ", p["src"])
            txt.insert("end", f"{p['raw']}{extra}\n")
        txt.config(state="disabled")

    def _on_aprs_select(self, _e=None):
        sel = self.aprs_tree.selection()
        if not sel:
            return
        self._aprs_selected = sel[0]
        for r in self._aprs_all():
            if f"{r['source']}:{r['callsign']}" == sel[0] and r["dist_km"] is not None:
                self.aprs_map.set_position(r["lat"], r["lon"])
                break

    # ---------------------------------------------------------------- AIS --

    # Catégories d'AIS-catcher (champ "shipclass") -> (libellé, couleur),
    # mêmes familles et couleurs proches de sa propre carte web.
    AIS_CLASSES = {
        2: ("Cargo", "#22c55e"),
        6: ("Pétrolier", "#ef4444"),
        4: ("Passagers", "#3b82f6"),
        7: ("Grande vitesse", "#eab308"),
        5: ("Remorqueur / spécial", "#a16207"),
        8: ("Pêche", "#f472b6"),
        3: ("Plaisance / classe B", "#a855f7"),
        0: ("Autre", "#06b6d4"),
        1: ("Autre", "#06b6d4"),
        12: ("Aide à la navigation", "#facc15"),
        11: ("Station de base", "#1d4ed8"),
        13: ("SART / balise", "#dc2626"),
        9: ("Avion SAR", "#f97316"),
        10: ("Hélicoptère SAR", "#f97316"),
    }
    AIS_FILTERS = [
        "Cargo", "Pétrolier", "Passagers", "Grande vitesse", "Remorqueur / spécial", "Pêche",
        "Plaisance / classe B", "Autre", "Aide à la navigation", "Station de base", "SART / balise",
        "Avion SAR", "Hélicoptère SAR",
    ]
    AIS_STATUS = {
        0: "En route au moteur", 1: "Au mouillage", 2: "Non manœuvrant", 3: "Manœuvre restreinte",
        4: "Gêné par son tirant d'eau", 5: "Amarré", 6: "Échoué", 7: "En pêche", 8: "En route à la voile",
        14: "SART actif",
    }
    AIS_RINGS_NM = (5, 10, 25, 50)

    def _build_ais_tab(self, frame):
        outer_wrapper = tk.Frame(frame, bg=theme.BG)
        outer_wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        left = tk.Frame(outer_wrapper, bg=theme.BG, width=640)
        left.pack(side="left", fill="y", pady=(0, 6))
        left.pack_propagate(False)

        # --- Statut + compteur + lien vers la carte web d'AIS-catcher
        status_outer = theme.card(left)
        status_outer.pack(fill="x", padx=12, pady=(8, 6))
        status_inner = status_outer.inner
        status_inner.configure(padding=12)
        tk.Label(
            status_inner, text="AIS (AIS-catcher)", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION,
        ).pack(side="left")
        pill = theme.pill(status_inner, "INCONNU", theme.MUTED)
        pill.pack(side="left", padx=(12, 0))
        if not hasattr(self, "_info_status_pills"):
            self._info_status_pills = {}
        self._info_status_pills["ais"] = pill
        ttk.Button(
            status_inner, text="Carte web AIS-catcher", style="Flat.TButton",
            command=lambda: self._open_url(
                f"http://{self.config_data.get('pi_host', '192.168.1.229')}:{self.config_data.get('port_ais', 8100)}/"
            ),
        ).pack(side="right")

        self.ais_count_label = tk.Label(
            left, text="Navires : —", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION,
            anchor="w", justify="left", wraplength=610,
        )
        self.ais_count_label.pack(anchor="w", padx=12, pady=(0, 4))

        # --- Recherche + filtres par catégorie
        filt_outer = theme.card(left)
        filt_outer.pack(fill="x", padx=12, pady=(0, 6))
        filt = filt_outer.inner
        filt.configure(padding=8)
        search_row = tk.Frame(filt, bg=theme.BG_CARD)
        search_row.pack(fill="x")
        tk.Label(
            search_row, text="Rechercher :", bg=theme.BG_CARD, fg=theme.MUTED,
            font=theme.FONT_SUBTITLE,
        ).pack(side="left", padx=(0, 6))
        self.ais_search_var = tk.StringVar()
        self.ais_search_var.trace_add("write", lambda *_a: self._repaint_ais())
        ttk.Entry(search_row, textvariable=self.ais_search_var, width=14).pack(side="left")
        for txt, val in (("Tout", True), ("Rien", False)):
            ttk.Button(
                search_row, text=txt, style="Flat.TButton",
                command=lambda v=val: self._ais_set_all_filters(v),
            ).pack(side="right", padx=(4, 0))

        grid = tk.Frame(filt, bg=theme.BG_CARD)
        grid.pack(fill="x", pady=(6, 0))
        self.ais_filter_vars = {}
        colors = {label: color for label, color in self.AIS_CLASSES.values()}
        for i, label in enumerate(self.AIS_FILTERS):
            var = tk.BooleanVar(value=True)
            var.trace_add("write", lambda *_a: self._repaint_ais())
            self.ais_filter_vars[label] = var
            cell = tk.Frame(grid, bg=theme.BG_CARD)
            cell.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 10), pady=1)
            tk.Label(cell, text="●", bg=theme.BG_CARD, fg=colors[label], font=("Segoe UI", 11)).pack(side="left")
            tk.Checkbutton(
                cell, text=label, variable=var, bg=theme.BG_CARD, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT,
                activebackground=theme.BG_CARD, activeforeground=theme.TEXT, font=("Segoe UI", 9),
                highlightthickness=0, bd=0,
            ).pack(side="left")
        self.ais_moving_only = tk.BooleanVar(value=False)
        self.ais_moving_only.trace_add("write", lambda *_a: self._repaint_ais())
        tk.Checkbutton(
            filt, text="Navires en mouvement uniquement (> 0,5 kn)", variable=self.ais_moving_only,
            bg=theme.BG_CARD, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG_CARD,
            activeforeground=theme.TEXT, font=("Segoe UI", 9), highlightthickness=0, bd=0,
        ).pack(anchor="w", pady=(4, 0))

        # --- Liste (triable par clic sur l'en-tête)
        table_outer = theme.card(left)
        table_outer.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        columns = ("country", "name", "dist", "speed", "type", "seen")
        self.ais_tree = ttk.Treeview(table_outer.inner, columns=columns, show="headings", height=7)
        headings = {
            "country": "Pays", "name": "Nom", "dist": "Dist (NM)", "speed": "Vit (kn)",
            "type": "Type", "seen": "Vu il y a",
        }
        widths = {"country": 50, "name": 170, "dist": 75, "speed": 65, "type": 150, "seen": 75}
        for col in columns:
            self.ais_tree.heading(col, text=headings[col], command=lambda c=col: self._ais_sort_by(c))
            self.ais_tree.column(col, width=widths[col], minwidth=40, anchor="w" if col in ("name", "type") else "center")
        vsb = ttk.Scrollbar(table_outer.inner, orient="vertical", command=self.ais_tree.yview)
        self.ais_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.ais_tree.pack(fill="both", expand=True, padx=1, pady=1)
        self.ais_tree.bind("<<TreeviewSelect>>", self._on_ais_select)
        self._ais_sort = ("dist", False)

        # --- Fiche détaillée
        detail_outer = theme.card(left)
        detail_outer.pack(fill="x", padx=12, pady=(0, 6))
        detail = detail_outer.inner
        detail.configure(padding=8)
        self.ais_detail_title = tk.Label(
            detail, text="Cliquez un navire (liste ou carte) pour voir sa fiche.",
            bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY_BOLD, anchor="w",
        )
        self.ais_detail_title.pack(anchor="w")
        fields = tk.Frame(detail, bg=theme.BG_CARD)
        fields.pack(anchor="w", fill="x", pady=(4, 0))
        titles = [
            ("mmsi", "MMSI"), ("callsign", "Indicatif"), ("imo", "IMO"), ("country", "Pavillon"),
            ("speed", "Vitesse"), ("cog", "Route (COG)"), ("heading", "Cap vrai"), ("status", "Statut"),
            ("dist", "Distance / relèvement"), ("dims", "Dimensions"), ("draught", "Tirant d'eau"), ("seen", "Vu il y a"),
            ("destination", "Destination"), ("eta", "ETA"), ("level", "Signal"), ("count", "Messages"),
        ]
        self.ais_detail_values = {}
        for i, (key, title) in enumerate(titles):
            r, c = divmod(i, 4)
            cell = tk.Frame(fields, bg=theme.BG_CARD)
            cell.grid(row=r, column=c, sticky="w", padx=(0, 14), pady=1)
            tk.Label(cell, text=title, bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", 8)).pack(anchor="w")
            v = tk.Label(cell, text="—", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY)
            v.pack(anchor="w")
            self.ais_detail_values[key] = v

        # --- Carte
        map_outer = theme.card(outer_wrapper)
        map_outer.pack(side="left", fill="both", expand=True, pady=(8, 12), padx=(0, 12))
        self.ais_map = self._make_map(map_outer.inner)
        self.ais_map.pack(fill="both", expand=True, padx=1, pady=1)
        lat, lon = DEFAULT_COORDS
        self.ais_map.set_position(lat, lon)
        self.ais_map.set_zoom(9)

        self._ais_ships = []
        self._ais_by_mmsi = {}
        self._ais_selected = None
        self._ais_station = DEFAULT_COORDS
        self._ais_rings_drawn_for = None
        self._ais_icon_cache = {}

    # -- Classement / formatage

    def _ais_class(self, ship):
        return self.AIS_CLASSES.get(ship.get("shipclass"), ("Autre", "#06b6d4"))

    @staticmethod
    def _ais_name(ship):
        return (ship.get("shipname") or "").strip() or str(ship.get("mmsi", "?"))

    @staticmethod
    def _ais_num(v):
        return v if isinstance(v, (int, float)) else None

    def _ais_set_all_filters(self, value):
        for var in self.ais_filter_vars.values():
            var.set(value)

    def _ais_sort_by(self, col):
        current, desc = self._ais_sort
        self._ais_sort = (col, not desc if current == col else False)
        self._repaint_ais()

    def _ais_icon(self, color, shape, angle):
        key = (color, shape, int(angle or 0) // 10 * 10)
        if key in self._ais_icon_cache:
            return self._ais_icon_cache[key]
        size = 20
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        c = size / 2
        if shape == "moving":
            d.polygon([(c, 1), (size - 4, size - 3), (c, size - 7), (4, size - 3)], fill=color, outline="#0b0f17")
            img = img.rotate(-key[2], resample=Image.BICUBIC)
        elif shape == "diamond":
            d.polygon([(c, 2), (size - 3, c), (c, size - 3), (3, c)], fill=color, outline="#0b0f17")
        else:
            d.ellipse([5, 5, size - 5, size - 5], fill=color, outline="#0b0f17")
        photo = ImageTk.PhotoImage(img)
        self._ais_icon_cache[key] = photo
        return photo

    def _ais_draw_rings(self):
        """Cercles de distance (5, 10, 25, 50 NM) autour de la station, comme
        sur la carte web d'AIS-catcher. Redessinés seulement si la position
        de la station change."""
        if self._ais_rings_drawn_for == self._ais_station:
            return
        self._ais_rings_drawn_for = self._ais_station
        self.ais_map.delete_all_polygon()
        lat0, lon0 = self._ais_station
        for nm in self.AIS_RINGS_NM:
            r_lat = nm / 60.0
            r_lon = r_lat / max(0.1, math.cos(math.radians(lat0)))
            pts = [
                (lat0 + r_lat * math.sin(math.radians(a)), lon0 + r_lon * math.cos(math.radians(a)))
                for a in range(0, 361, 6)
            ]
            self.ais_map.set_polygon(pts, outline_color="#3b82f6", fill_color=None, border_width=1)

    # -- Données

    def _render_ais(self, data, error):
        if error:
            self._ais_ships = []
            if getattr(self, "_sdr_mode", None) not in (None, "ais"):
                label = SDR_MODE_LABELS.get(self._sdr_mode, self._sdr_mode)
                txt = ("Navires : AIS en pause — la clé SDR est arrêtée. Ouvrez cette page pour la récupérer."
                       if self._sdr_mode == "off" else
                       f"Navires : AIS en pause — la clé SDR est sur {label}. Ouvrez cette page pour la récupérer.")
            else:
                txt = "Navires : indisponible — AIS-catcher est-il démarré sur le Pi ?"
            self.ais_count_label.config(text=txt)
            if "ais" in self.tile_chips:
                self.tile_chips["ais"].config(text="—")
            self._repaint_ais(keep_label=True)
            return

        ships = data.get("ships", []) if isinstance(data, dict) else []
        self._ais_ships = ships
        self._check_ais_alerts(ships)
        self._ais_by_mmsi = {s.get("mmsi"): s for s in ships}
        station = data.get("station") if isinstance(data, dict) else None
        if isinstance(station, dict):
            slat, slon = self._ais_num(station.get("lat")), self._ais_num(station.get("lon"))
            if slat is not None and slon is not None and not (slat == 0 and slon == 0):
                self._ais_station = (slat, slon)
        if "ais" in self.tile_chips:
            self.tile_chips["ais"].config(text=f"{len(ships)} navire(s)")
        self._repaint_ais()
        if self._ais_selected in self._ais_by_mmsi:
            self._show_ais_detail(self._ais_by_mmsi[self._ais_selected])

    def _ais_visible(self, ship):
        label, _color = self._ais_class(ship)
        if not self.ais_filter_vars.get(label, tk.BooleanVar(value=True)).get():
            return False
        if self.ais_moving_only.get() and not ((self._ais_num(ship.get("speed")) or 0) > 0.5):
            return False
        q = self.ais_search_var.get().strip().lower()
        if q:
            hay = f"{ship.get('shipname', '')} {ship.get('mmsi', '')} {ship.get('callsign', '')}".lower()
            return q in hay
        return True

    def _repaint_ais(self, keep_label=False):
        if not hasattr(self, "ais_tree"):
            return
        ships = [s for s in self._ais_ships if self._ais_visible(s)]
        col, desc = self._ais_sort
        def key(s):
            if col == "name":
                return self._ais_name(s).lower()
            if col == "country":
                return s.get("country") or ""
            if col == "type":
                return self._ais_class(s)[0]
            field = {"dist": "distance", "speed": "speed", "seen": "last_signal"}[col]
            v = self._ais_num(s.get(field))
            return v if v is not None else float("inf")
        ships.sort(key=key, reverse=desc)

        if not keep_label:
            with_pos = sum(1 for s in self._ais_ships if self._ais_num(s.get("lat")) is not None)
            shown = f" — {len(ships)} affiché(s)" if len(ships) != len(self._ais_ships) else ""
            self.ais_count_label.config(
                text=f"Navires : {len(self._ais_ships)} (dont {with_pos} positionnés){shown}",
            )

        self.ais_tree.delete(*self.ais_tree.get_children())
        for s in ships:
            dist = self._ais_num(s.get("distance"))
            speed = self._ais_num(s.get("speed"))
            seen = self._ais_num(s.get("last_signal"))
            seen_txt = "—" if seen is None else (f"{int(seen)} s" if seen < 60 else f"{int(seen // 60)} min {int(seen % 60):02d} s")
            iid = str(s.get("mmsi"))
            if self.ais_tree.exists(iid):
                continue
            self.ais_tree.insert(
                "", "end", iid=iid,
                values=(
                    s.get("country") or "—", self._ais_name(s),
                    "—" if dist is None else f"{dist:.1f}", "—" if speed is None else f"{speed:.1f}",
                    self._ais_class(s)[0], seen_txt,
                ),
            )
        if self._ais_selected is not None and self.ais_tree.exists(str(self._ais_selected)):
            self.ais_tree.selection_set(str(self._ais_selected))

        self.ais_map.delete_all_marker()
        self._ais_draw_rings()
        self.ais_map.set_marker(*self._ais_station, text="F4JCF", text_color="#16a34a",
                                marker_color_circle="#14532d", marker_color_outside="#22c55e")
        for s in ships:
            lat, lon = self._ais_num(s.get("lat")), self._ais_num(s.get("lon"))
            if lat is None or lon is None:
                continue
            label, color = self._ais_class(s)
            moving = (self._ais_num(s.get("speed")) or 0) > 0.5
            cls = s.get("shipclass")
            shape = "diamond" if cls in (11, 12, 13) else ("moving" if moving else "still")
            angle = self._ais_num(s.get("cog")) if moving else 0
            selected = s.get("mmsi") == self._ais_selected
            self.ais_map.set_marker(
                lat, lon, text=self._ais_name(s) if selected else None,
                icon=self._ais_icon(color, shape, angle), data=s.get("mmsi"),
                command=self._on_ais_marker_click,
            )

    def _on_ais_marker_click(self, marker):
        ship = self._ais_by_mmsi.get(marker.data)
        if ship:
            self._ais_selected = marker.data
            self._show_ais_detail(ship)
            self._repaint_ais()

    def _on_ais_select(self, _event=None):
        sel = self.ais_tree.selection()
        if not sel:
            return
        try:
            mmsi = int(sel[0])
        except ValueError:
            return
        if mmsi == self._ais_selected:
            return
        ship = self._ais_by_mmsi.get(mmsi)
        if not ship:
            return
        self._ais_selected = mmsi
        self._show_ais_detail(ship)
        lat, lon = self._ais_num(ship.get("lat")), self._ais_num(ship.get("lon"))
        if lat is not None and lon is not None:
            self.ais_map.set_position(lat, lon)
        self._repaint_ais()

    def _show_ais_detail(self, s):
        num = self._ais_num
        label, _ = self._ais_class(s)
        self.ais_detail_title.config(text=f"{self._ais_name(s)}  —  {label}")
        def put(key, txt):
            self.ais_detail_values[key].config(text=txt if txt not in (None, "") else "—")
        put("mmsi", s.get("mmsi"))
        put("callsign", (s.get("callsign") or "").strip())
        put("imo", s.get("imo"))
        put("country", s.get("country"))
        put("speed", None if num(s.get("speed")) is None else f"{s['speed']:.1f} kn")
        put("cog", None if num(s.get("cog")) is None else f"{s['cog']:.0f}°")
        put("heading", None if num(s.get("heading")) is None else f"{s['heading']:.0f}°")
        put("status", self.AIS_STATUS.get(s.get("status")))
        dist, brg = num(s.get("distance")), num(s.get("bearing"))
        put("dist", None if dist is None else f"{dist:.1f} NM" + ("" if brg is None else f" / {brg:.0f}°"))
        dims = [num(s.get(k)) for k in ("to_bow", "to_stern", "to_port", "to_starboard")]
        put("dims", None if None in dims else f"{dims[0] + dims[1]:.0f} × {dims[2] + dims[3]:.0f} m")
        put("draught", None if num(s.get("draught")) is None else f"{s['draught']:.1f} m")
        seen = num(s.get("last_signal"))
        put("seen", None if seen is None else f"{int(seen)} s")
        put("destination", (s.get("destination") or "").strip())
        eta = [s.get(k) for k in ("eta_day", "eta_month", "eta_hour", "eta_minute")]
        try:
            eta_txt = None if None in eta else "{:02d}/{:02d} {:02d}:{:02d}".format(*(int(x) for x in eta))
        except (TypeError, ValueError):
            eta_txt = None
        put("eta", eta_txt)
        put("level", None if num(s.get("level")) is None else f"{s['level']:.1f} dB")
        put("count", s.get("count"))

    # ------------------------------------------------------------ SvxLink --

    def _build_svxlink_tab(self, frame):
        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        # --- En-tête : statut du service, indicatif, modules
        head_outer = theme.card(wrapper)
        head_outer.pack(fill="x", padx=12, pady=(8, 8))
        head = head_outer.inner
        head.configure(padding=14)
        tk.Label(head, text="SvxLink / EchoLink", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION).pack(side="left")
        pill = theme.pill(head, "INCONNU", theme.MUTED)
        pill.pack(side="left", padx=(12, 0))
        if not hasattr(self, "_info_status_pills"):
            self._info_status_pills = {}
        self._info_status_pills["svxlink"] = pill
        self.svx_ident_label = tk.Label(head, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY)
        self.svx_ident_label.pack(side="left", padx=(18, 0))
        ttk.Button(
            head, text="Redémarrer SvxLink", style="Flat.TButton",
            command=lambda: self._restart_service(self._svx_service_key()),
        ).pack(side="right")

        # --- Voyants en direct
        tiles = tk.Frame(wrapper, bg=theme.BG)
        tiles.pack(fill="x", padx=6)
        self.svx_tiles = {}
        for i, (key, title) in enumerate((
            ("tx", "ÉMISSION"), ("rx", "RÉCEPTION (squelch)"),
            ("module", "MODULE ACTIF"), ("dir", "ANNUAIRE ECHOLINK"),
        )):
            tiles.grid_columnconfigure(i, weight=1, uniform="svx")
            outer = theme.card(tiles)
            outer.grid(row=0, column=i, sticky="nsew", padx=6, pady=(0, 8))
            inner = outer.inner
            inner.configure(padding=14)
            tk.Label(inner, text=title, bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
            value = tk.Label(inner, text="—", bg=theme.BG_CARD, fg=theme.TEXT, font=("Segoe UI", 18, "bold"))
            value.pack(anchor="w", pady=(4, 0))
            sub = tk.Label(inner, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
            sub.pack(anchor="w")
            self.svx_tiles[key] = (value, sub)

        # --- Avertissements (masqués s'il n'y en a pas)
        self.svx_warn_outer = theme.card(wrapper)
        self.svx_warn_label = tk.Label(
            self.svx_warn_outer.inner, text="", bg=theme.BG_CARD, fg=theme.ORANGE, font=theme.FONT_BODY,
            justify="left", anchor="w", wraplength=1100,
        )
        self.svx_warn_label.pack(anchor="w", padx=14, pady=10)
        self._svx_warn_shown = False

        # --- Deux colonnes : stations connectées | journal d'activité
        cols = tk.Frame(wrapper, bg=theme.BG)
        cols.pack(fill="both", expand=True, padx=6, pady=(0, 12))
        self.svx_cols = cols
        cols.grid_columnconfigure(0, weight=1)
        cols.grid_columnconfigure(1, weight=2)
        cols.grid_rowconfigure(0, weight=1)

        left_outer = theme.card(cols)
        left_outer.grid(row=0, column=0, sticky="nsew", padx=6)
        left = left_outer.inner
        left.configure(padding=12)
        tk.Label(left, text="Stations EchoLink connectées", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION).pack(anchor="w")
        self.svx_conn_tree = ttk.Treeview(left, columns=("call", "since"), show="headings", height=6)
        self.svx_conn_tree.heading("call", text="Indicatif")
        self.svx_conn_tree.heading("since", text="Connecté depuis")
        self.svx_conn_tree.column("call", width=140, anchor="w")
        self.svx_conn_tree.column("since", width=120, anchor="center")
        self.svx_conn_tree.pack(fill="both", expand=True, pady=(8, 8))
        self.svx_last_label = tk.Label(
            left, text="Dernière station : —", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY, anchor="w",
        )
        self.svx_last_label.pack(anchor="w")

        right_outer = theme.card(cols)
        right_outer.grid(row=0, column=1, sticky="nsew", padx=6)
        right = right_outer.inner
        right.configure(padding=12)
        tk.Label(right, text="Journal d'activité", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION).pack(anchor="w")
        self.svx_events_tree = ttk.Treeview(right, columns=("time", "event"), show="headings", height=12)
        self.svx_events_tree.heading("time", text="Heure")
        self.svx_events_tree.heading("event", text="Événement")
        self.svx_events_tree.column("time", width=130, anchor="center")
        self.svx_events_tree.column("event", width=420, anchor="w")
        vsb = ttk.Scrollbar(right, orient="vertical", command=self.svx_events_tree.yview)
        self.svx_events_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y", pady=(8, 0))
        self.svx_events_tree.pack(fill="both", expand=True, pady=(8, 0))

        self._svx_poll_running = False

    def _svx_service_key(self):
        """Clé exacte du service SvxLink dans station_api.py (inconnue à
        l'avance), retrouvée par son nom ; "svxlink" par défaut."""
        for key, info in (getattr(self, "_last_services", None) or {}).items():
            label = str(info.get("label", key)).lower()
            if "svx" in key.lower() or "svx" in label:
                return key
        return "svxlink"

    def _svx_poll(self):
        """Rafraîchissement rapide (2 s) tant que la page SvxLink est
        affichée, pour que les voyants émission / réception suivent le
        trafic en direct ; le reste de l'appli garde son rythme normal."""
        if getattr(self, "_current_view", None) != "svxlink":
            self._svx_poll_running = False
            return
        host = self.config_data.get("pi_host", "192.168.1.229")
        port = self.config_data.get("port_svxlink", 5007)
        threading.Thread(
            target=self._fetch, args=(lambda: api_client.get_svxlink_status(host, port), self._render_svxlink),
            daemon=True,
        ).start()
        self.after(2000, self._svx_poll)

    @staticmethod
    def _svx_clock(t):
        if not t:
            return "—"
        lt = time.localtime(t)
        if time.strftime("%Y%m%d", lt) == time.strftime("%Y%m%d"):
            return time.strftime("%H:%M:%S", lt)
        return time.strftime("%d/%m %H:%M", lt)

    @staticmethod
    def _svx_duration(seconds):
        seconds = max(0, int(seconds))
        if seconds < 60:
            return f"{seconds} s"
        if seconds < 3600:
            return f"{seconds // 60} min {seconds % 60:02d} s"
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"

    def _render_svxlink(self, data, error):
        if not hasattr(self, "svx_tiles"):
            return
        if error:
            for value, sub in self.svx_tiles.values():
                value.config(text="—", fg=theme.MUTED)
                sub.config(text="")
            self.svx_ident_label.config(
                text="Activité indisponible — svxlink-api est-il installé sur le Pi ?", fg=theme.RED,
            )
            return

        now = data.get("now") or time.time()
        call = data.get("callsign") or "—"
        modules = ", ".join(data.get("modules") or []) or "—"
        logic = ", ".join(data.get("logics") or []) or "—"
        self.svx_ident_label.config(text=f"{call}   ·   {logic}   ·   Modules : {modules}", fg=theme.MUTED)

        value, sub = self.svx_tiles["tx"]
        if data.get("tx"):
            value.config(text="EN ÉMISSION", fg=theme.RED)
            sub.config(text=f"depuis {self._svx_duration(now - (data.get('tx_since') or now))}")
        else:
            value.config(text="Repos", fg=theme.TEXT)
            last = data.get("last_tx") or {}
            sub.config(text=f"dernière : {self._svx_clock(last.get('end'))}" if last else "aucune émission récente")

        value, sub = self.svx_tiles["rx"]
        if data.get("squelch"):
            value.config(text="SIGNAL REÇU", fg=theme.GREEN)
            sub.config(text=f"depuis {self._svx_duration(now - (data.get('squelch_since') or now))}")
        else:
            value.config(text="Fermé", fg=theme.TEXT)
            last = data.get("last_rx") or {}
            sub.config(text=f"dernière : {self._svx_clock(last.get('end'))}" if last else "aucune réception récente")

        value, sub = self.svx_tiles["module"]
        mod = data.get("active_module")
        value.config(text=mod or "Aucun", fg=theme.ACCENT if mod else theme.TEXT)
        sub.config(text="activé par DTMF" if mod else "logique au repos")

        value, sub = self.svx_tiles["dir"]
        d = data.get("echolink_directory")
        value.config(
            text={"ON": "Connecté", "OFF": "Déconnecté"}.get(d, d or "—"),
            fg=theme.GREEN if d == "ON" else (theme.RED if d == "OFF" else theme.MUTED),
        )
        sub.config(text=f"{len(data.get('connected') or [])} station(s) en QSO")

        # Stations connectées
        self.svx_conn_tree.delete(*self.svx_conn_tree.get_children())
        for st in data.get("connected") or []:
            self.svx_conn_tree.insert(
                "", "end", values=(st.get("callsign"), self._svx_duration(now - (st.get("since") or now))),
            )
        last = data.get("last_station")
        self.svx_last_label.config(
            text=f"Dernière station : {last}  ({self._svx_clock(data.get('last_station_at'))})" if last
            else "Dernière station : —",
        )

        # Journal (le plus récent en haut)
        self.svx_events_tree.delete(*self.svx_events_tree.get_children())
        for ev in data.get("events") or []:
            self.svx_events_tree.insert("", "end", values=(self._svx_clock(ev.get("time")), ev.get("text", "")))

        # Avertissements
        warnings = []
        if not data.get("log_found", True):
            warnings.append("⚠ Journal /var/log/svxlink introuvable : l'activité ne peut pas être suivie.")
        dist_at = data.get("distortion_at")
        if dist_at and now - dist_at < 24 * 3600:
            warnings.append(
                f"⚠ Entrée audio saturée (« Distortion detected », dernière fois {self._svx_clock(dist_at)}) : "
                "baissez le niveau d'entrée de la carte son (alsamixer, réglage Capture) jusqu'à ce que le message disparaisse."
            )
        if data.get("missing_audio_count"):
            warnings.append(
                f"⚠ {data['missing_audio_count']} annonce(s) vocale(s) introuvable(s) : le pack de voix de SvxLink "
                "n'est pas installé ou ne correspond pas à la langue configurée — l'identification vocale reste muette."
            )
        if warnings:
            self.svx_warn_label.config(text="\n".join(warnings))
            if not self._svx_warn_shown:
                self.svx_warn_outer.pack(fill="x", padx=12, pady=(0, 8), before=self.svx_cols)
                self._svx_warn_shown = True
        elif self._svx_warn_shown:
            self.svx_warn_outer.pack_forget()
            self._svx_warn_shown = False

    # ------------------------------------------------------------ Alertes --

    SQUAWK_ALERTS = {
        "7700": "Détresse (urgence générale)",
        "7600": "Panne radio",
        "7500": "Intrusion illicite (détournement)",
    }

    def _alert_init(self):
        self._alerts = []                 # historique (plus récent en premier)
        self._alert_seen = {}             # clé -> horodatage du dernier signalement
        self._alert_window = None
        self._alert_started = time.time()

    def _raise_alert(self, key, title, text, cooldown=1800):
        """Signale un événement de sécurité : fenêtre au premier plan, son,
        pastille dans le bandeau et historique. Une même alerte (même clé)
        n'est re-signalée qu'après `cooldown` secondes."""
        now = time.time()
        last = self._alert_seen.get(key)
        self._alert_seen[key] = now
        if last is not None and now - last < cooldown:
            return
        self._alerts.insert(0, {"time": now, "title": title, "text": text})
        del self._alerts[100:]
        self._update_alert_chip()
        if self.config_data.get("alert_sound", True):
            self._alert_sound()
        self._tray_notify(f"⚠ {title}", text)
        self._show_alert_popup(title, text)

    def _alert_sound(self):
        def play():
            try:
                import winsound
                for _ in range(3):
                    winsound.Beep(1200, 250)
                    winsound.Beep(800, 250)
            except Exception:
                try:
                    self.after(0, self.bell)
                except Exception:
                    pass
        threading.Thread(target=play, daemon=True).start()

    def _show_alert_popup(self, title, text):
        win = tk.Toplevel(self)
        win.title(f"Alerte — {title}")
        win.configure(bg=theme.BG_CARD)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        head = tk.Frame(win, bg=theme.RED)
        head.pack(fill="x")
        tk.Label(head, text=f"⚠  {title}", bg=theme.RED, fg="#0b0f17", font=("Segoe UI", 14, "bold"),
                 padx=16, pady=10).pack(anchor="w")
        tk.Label(win, text=text, bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY, justify="left",
                 wraplength=460, padx=16, pady=14).pack(anchor="w")
        tk.Label(win, text=time.strftime("Le %d/%m/%Y à %H:%M:%S"), bg=theme.BG_CARD, fg=theme.MUTED,
                 font=theme.FONT_SUBTITLE, padx=16).pack(anchor="w")
        row = tk.Frame(win, bg=theme.BG_CARD)
        row.pack(fill="x", padx=16, pady=14)
        ttk.Button(row, text="OK", style="Accent.TButton", command=win.destroy).pack(side="right")
        ttk.Button(row, text="Historique des alertes", style="Flat.TButton",
                   command=lambda: (win.destroy(), self._show_alert_history())).pack(side="right", padx=(0, 8))
        self._alert_popups = [w for w in getattr(self, "_alert_popups", []) if w.winfo_exists()] + [win]
        offset = (len(self._alert_popups) - 1) * 36
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2 + offset
        y = self.winfo_rooty() + 120 + offset
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            win.lift()
            win.focus_force()
        except tk.TclError:
            pass

    def _update_alert_chip(self):
        recent = [a for a in self._alerts if time.time() - a["time"] < 24 * 3600]
        if recent:
            self.chip_alerts.config(text=f" ⚠ Alertes : {len(recent)} ", bg=theme.RED, fg="#0b0f17")
        else:
            self.chip_alerts.config(text=" Alertes : 0 ", bg=theme.BG_CARD_ALT, fg=theme.TEXT)
        if "alerts" in getattr(self, "tile_chips", {}):
            self.tile_chips["alerts"].config(
                text=(f"⚠ {len(recent)} alerte(s) sur 24 h" if recent else "Aucune alerte"),
            )
        self._refresh_alerts_page()

    def _show_alert_history(self):
        self.show_page("alerts")

    def _build_alerts_tab(self, frame):
        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)
        head_outer = theme.card(wrapper)
        head_outer.pack(fill="x", padx=12, pady=(8, 8))
        head = head_outer.inner
        head.configure(padding=14)
        tk.Label(head, text="Alertes de sécurité", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION).pack(side="left")
        self.alerts_summary = tk.Label(head, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY)
        self.alerts_summary.pack(side="left", padx=(16, 0))
        ttk.Button(head, text="Effacer l'historique", style="Flat.TButton", command=self._clear_alerts).pack(side="right")
        ttk.Button(head, text="Tester l'alerte", style="Flat.TButton",
                   command=lambda: self._raise_alert(f"test:{time.time()}", "Test d'alerte",
                                                     "Ceci est un test : la fenêtre et le son fonctionnent.", cooldown=0),
                   ).pack(side="right", padx=(0, 8))
        tk.Checkbutton(
            head, text="Son", variable=self._alert_sound_var, command=self._toggle_alert_sound,
            bg=theme.BG_CARD, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG_CARD,
            activeforeground=theme.TEXT, font=theme.FONT_BODY, highlightthickness=0,
        ).pack(side="right", padx=(0, 12))

        info_outer = theme.card(wrapper)
        info_outer.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(
            info_outer.inner, text=(
                "Surveillé en permanence : codes transpondeur 7700 (détresse), 7600 (panne radio), 7500 (intrusion "
                "illicite) et états d'urgence ADS-B  ·  balises AIS SART / EPIRB / homme à la mer  ·  messages APRS "
                "adressés à F4JCF  ·  nouveaux courriers Winlink  ·  vigilance Météo-France orange ou rouge (06).\nAvion et AIS ne sont surveillés que lorsque leur "
                "module détient la clé SDR (voir la page Clé SDR pour le partage automatique)."
            ), bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE, justify="left", wraplength=1300,
        ).pack(anchor="w", padx=14, pady=10)

        self._build_notify_card(wrapper)

        table_outer = theme.card(wrapper)
        table_outer.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.alerts_tree = ttk.Treeview(table_outer.inner, columns=("time", "title", "text"), show="headings")
        for col, txt, w in (("time", "Date", 140), ("title", "Alerte", 300), ("text", "Détail", 760)):
            self.alerts_tree.heading(col, text=txt)
            self.alerts_tree.column(col, width=w, anchor="w")
        self.alerts_tree.tag_configure("recent", foreground=theme.RED)
        self.alerts_tree.pack(fill="both", expand=True, padx=1, pady=1)
        self._refresh_alerts_page()

    def _refresh_alerts_page(self):
        if not hasattr(self, "alerts_tree"):
            return
        self.alerts_tree.delete(*self.alerts_tree.get_children())
        now = time.time()
        for a in self._alerts:
            self.alerts_tree.insert(
                "", "end", values=(time.strftime("%d/%m %H:%M:%S", time.localtime(a["time"])),
                                   a["title"], a["text"].replace("\n", " · ")),
                tags=("recent",) if now - a["time"] < 3600 else (),
            )
        if not self._alerts:
            self.alerts_tree.insert("", "end", values=("", "Aucune alerte depuis le lancement de l'appli", ""))
        recent = sum(1 for a in self._alerts if now - a["time"] < 24 * 3600)
        self.alerts_summary.config(text=f"{recent} alerte(s) ces dernières 24 h" if recent else "Aucune alerte récente")

    def _clear_alerts(self):
        self._alerts.clear()
        self._update_alert_chip()

    def _toggle_alert_sound(self):
        self.config_data["alert_sound"] = bool(self._alert_sound_var.get())
        try:
            save_config(self.config_data)
        except Exception:
            pass

    # -- Détections

    def _check_aircraft_alerts(self, aircraft):
        for ac in aircraft:
            sq = str(ac.get("squawk") or "").strip()
            if sq in self.SQUAWK_ALERTS:
                hx = ac.get("hex", "?")
                call = (ac.get("flight") or "").strip() or hx.upper()
                alt = ac.get("alt_baro")
                pos = ""
                if ac.get("lat") is not None and ac.get("lon") is not None:
                    pos = f"\nPosition : {ac['lat']:.4f}, {ac['lon']:.4f}"
                self._raise_alert(
                    f"squawk:{hx}:{sq}", f"Squawk {sq} — {call}",
                    f"L'avion {call} (hex {hx}) affiche le code transpondeur {sq} : "
                    f"{self.SQUAWK_ALERTS[sq]}.\nAltitude : {alt if alt is not None else '—'} ft{pos}",
                )
            if ac.get("emergency") not in (None, "", "none") and sq not in self.SQUAWK_ALERTS:
                hx = ac.get("hex", "?")
                call = (ac.get("flight") or "").strip() or hx.upper()
                self._raise_alert(
                    f"emerg:{hx}:{ac.get('emergency')}", f"Urgence ADS-B — {call}",
                    f"L'avion {call} (hex {hx}) signale un état d'urgence : {ac.get('emergency')}.",
                )

    def _check_ais_alerts(self, ships):
        for s in ships:
            mmsi = s.get("mmsi")
            try:
                m = int(mmsi)
            except (TypeError, ValueError):
                continue
            kind = None
            if 970000000 <= m <= 970999999 or s.get("shipclass") == 13:
                kind = "Balise SART (radeau / naufragé)"
            elif 972000000 <= m <= 972999999:
                kind = "Balise homme à la mer (MOB)"
            elif 974000000 <= m <= 974999999:
                kind = "Balise de détresse EPIRB"
            elif s.get("status") == 14:
                kind = "Signal SART actif"
            if kind:
                pos = ""
                if isinstance(s.get("lat"), (int, float)) and isinstance(s.get("lon"), (int, float)):
                    pos = f"\nPosition : {s['lat']:.4f}, {s['lon']:.4f}"
                    if isinstance(s.get("distance"), (int, float)):
                        pos += f"  ({s['distance']:.1f} NM)"
                self._raise_alert(
                    f"ais:{m}", f"AIS — {kind}",
                    f"Émission AIS de détresse reçue : MMSI {m}.{pos}\n"
                    "Une balise SART/EPIRB/MOB peut aussi être en test : vérifiez avant d'alerter.",
                    cooldown=3600,
                )

    def _render_aprs_packets(self, data, error):
        if error or not isinstance(data, list):
            return
        mycall = str(self.config_data.get("aprs_callsign", "F4JCF")).upper()
        for p in data:
            raw = str(p.get("raw") or "")
            if len(raw) < 11 or raw[0] != ":" or raw[10] != ":":
                continue
            addressee = raw[1:10].strip().upper()
            if addressee.split("-")[0] != mycall:
                continue
            body = raw[11:]
            if body.startswith(("ack", "rej")):
                continue
            msg, _, msg_id = body.partition("{")
            heard = p.get("last_heard") or time.time()
            if heard < self._alert_started - 1800:
                continue
            src = p.get("callsign", "?")
            self._raise_alert(
                f"aprsmsg:{src}:{msg_id or msg}", f"Message APRS de {src}",
                f"Pour {addressee} : {msg.strip()}\nReçu à {time.strftime('%H:%M:%S', time.localtime(heard))}",
                cooldown=24 * 3600,
            )

    # ------------------------------------------------------------ Winlink --

    def _build_winlink_tab(self, frame):
        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        head_outer = theme.card(wrapper)
        head_outer.pack(fill="x", padx=12, pady=(8, 8))
        head = head_outer.inner
        head.configure(padding=14)
        tk.Label(head, text="Winlink (Pat)", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION).pack(side="left")
        self.wl_pill = theme.pill(head, "INCONNU", theme.MUTED)
        self.wl_pill.pack(side="left", padx=(12, 0))
        self.wl_info = tk.Label(head, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY)
        self.wl_info.pack(side="left", padx=(16, 0))
        ttk.Button(
            head, text="Ouvrir Pat dans le navigateur", style="Flat.TButton",
            command=lambda: self._open_url(
                f"http://{self.config_data.get('pi_host', '192.168.1.229')}:{self.config_data.get('port_winlink', 8080)}/ui"
            ),
        ).pack(side="right")
        self.wl_connect_btn = ttk.Button(
            head, text="Relever le courrier (Internet)", style="Accent.TButton", command=self._winlink_connect,
        )
        self.wl_connect_btn.pack(side="right", padx=(0, 8))

        tiles = tk.Frame(wrapper, bg=theme.BG)
        tiles.pack(fill="x", padx=6)
        self.wl_tiles = {}
        for i, (key, title) in enumerate((
            ("conn", "CONNEXION"), ("in", "BOÎTE DE RÉCEPTION"), ("out", "BOÎTE D'ENVOI"), ("sent", "ENVOYÉS"),
        )):
            tiles.grid_columnconfigure(i, weight=1, uniform="wl")
            outer = theme.card(tiles)
            outer.grid(row=0, column=i, sticky="nsew", padx=6, pady=(0, 8))
            inner = outer.inner
            inner.configure(padding=14)
            tk.Label(inner, text=title, bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
            v = tk.Label(inner, text="—", bg=theme.BG_CARD, fg=theme.TEXT, font=("Segoe UI", 18, "bold"))
            v.pack(anchor="w", pady=(4, 0))
            s = tk.Label(inner, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
            s.pack(anchor="w")
            self.wl_tiles[key] = (v, s)

        lists = tk.Frame(wrapper, bg=theme.BG)
        lists.pack(fill="both", expand=True, padx=6, pady=(0, 12))
        lists.grid_columnconfigure(0, weight=3)
        lists.grid_columnconfigure(1, weight=2)
        lists.grid_rowconfigure(0, weight=1)
        self.wl_trees = {}
        for col, (box, title) in enumerate((("in", "Messages reçus (double-clic pour lire)"), ("out", "En attente d'envoi"))):
            outer = theme.card(lists)
            outer.grid(row=0, column=col, sticky="nsew", padx=6)
            inner = outer.inner
            inner.configure(padding=12)
            tk.Label(inner, text=title, bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION).pack(anchor="w")
            tree = ttk.Treeview(inner, columns=("date", "who", "subject"), show="headings", height=12)
            tree.heading("date", text="Date")
            tree.heading("who", text="De" if box == "in" else "À")
            tree.heading("subject", text="Objet")
            tree.column("date", width=110, anchor="center")
            tree.column("who", width=110, anchor="w")
            tree.column("subject", width=300, anchor="w")
            tree.tag_configure("unread", foreground=theme.ACCENT)
            tree.pack(fill="both", expand=True, pady=(8, 0))
            tree.bind("<Double-1>", lambda _e, b=box: self._winlink_open_message(b))
            self.wl_trees[box] = tree
        self._wl_known_inbox = None

    @staticmethod
    def _wl_addr(a):
        if isinstance(a, dict):
            return a.get("Addr") or a.get("addr") or ""
        return str(a or "")

    @staticmethod
    def _wl_date(txt):
        if not txt:
            return "—"
        try:
            dt = datetime.fromisoformat(str(txt).replace("Z", "+00:00"))
            return dt.astimezone().strftime("%d/%m %H:%M")
        except ValueError:
            return str(txt)[:16]

    def _fetch_winlink(self):
        host = self.config_data.get("pi_host", "192.168.1.229")
        port = self.config_data.get("port_winlink", 8080)
        try:
            data = {
                "status": api_client.get_pat_status(host, port),
                "in": api_client.get_pat_mailbox(host, port, "in"),
                "out": api_client.get_pat_mailbox(host, port, "out"),
                "sent": api_client.get_pat_mailbox(host, port, "sent"),
            }
        except Exception as exc:
            self.after(0, self._render_winlink, None, str(exc))
            return
        self.after(0, self._render_winlink, data, None)

    def _render_winlink(self, data, error):
        if not hasattr(self, "wl_tiles"):
            return
        if error:
            self.wl_pill.config(text="  NON INSTALLÉ / INJOIGNABLE  ", bg=theme.MUTED)
            self.wl_info.config(text="Pat ne répond pas sur le port 8080 — installez-le avec install_winlink.sh.")
            if "winlink" in self.tile_chips:
                self.tile_chips["winlink"].config(text="Non configuré")
            return
        st = data.get("status") or {}
        inbox, outbox, sent = data.get("in") or [], data.get("out") or [], data.get("sent") or []
        if st.get("connected"):
            pill, col = "CONNECTÉ", theme.GREEN
        elif st.get("dialing"):
            pill, col = "APPEL EN COURS", theme.ORANGE
        else:
            pill, col = "EN SERVICE", theme.GREEN
        self.wl_pill.config(text=f"  {pill}  ", bg=col)
        listeners = ", ".join(st.get("active_listeners") or []) or "aucune"
        self.wl_info.config(text=f"Écoute (P2P / RMS) : {listeners}")

        v, s = self.wl_tiles["conn"]
        if st.get("connected"):
            v.config(text="Connecté", fg=theme.GREEN)
            s.config(text=st.get("remote_addr") or "")
        elif st.get("dialing"):
            v.config(text="Appel…", fg=theme.ORANGE)
            s.config(text="")
        else:
            v.config(text="Libre", fg=theme.TEXT)
            s.config(text="prêt à relever le courrier")
        unread = sum(1 for m in inbox if m.get("Unread"))
        v, s = self.wl_tiles["in"]
        v.config(text=str(len(inbox)), fg=theme.ACCENT if unread else theme.TEXT)
        s.config(text=f"dont {unread} non lu(s)" if unread else "aucun message non lu")
        v, s = self.wl_tiles["out"]
        v.config(text=str(len(outbox)), fg=theme.ORANGE if outbox else theme.TEXT)
        s.config(text="partiront au prochain relevé" if outbox else "rien en attente")
        v, s = self.wl_tiles["sent"]
        v.config(text=str(len(sent)), fg=theme.TEXT)
        s.config(text=f"dernier : {self._wl_date(sent[0].get('Date'))}" if sent else "")

        for box, msgs in (("in", inbox), ("out", outbox)):
            tree = self.wl_trees[box]
            sel = tree.selection()
            tree.delete(*tree.get_children())
            for m in msgs:
                who = self._wl_addr(m.get("From")) if box == "in" else ", ".join(self._wl_addr(a) for a in (m.get("To") or []))
                mid = m.get("MID") or ""
                if not mid or tree.exists(mid):
                    continue
                tree.insert("", "end", iid=mid, values=(self._wl_date(m.get("Date")), who, m.get("Subject") or ""),
                            tags=("unread",) if m.get("Unread") else ())
            for iid in sel:
                if tree.exists(iid):
                    tree.selection_add(iid)

        # Nouveaux messages reçus depuis le dernier passage
        mids = {m.get("MID") for m in inbox}
        if self._wl_known_inbox is not None:
            for m in inbox:
                if m.get("MID") not in self._wl_known_inbox:
                    self._raise_alert(
                        f"winlink:{m.get('MID')}", f"Winlink — message de {self._wl_addr(m.get('From'))}",
                        f"Objet : {m.get('Subject') or '(sans objet)'}", cooldown=10 ** 9,
                    )
        self._wl_known_inbox = mids
        if "winlink" in self.tile_chips:
            self.tile_chips["winlink"].config(
                text=(f"{unread} non lu(s)" if unread else f"{len(inbox)} message(s)") + (f" · {len(outbox)} à envoyer" if outbox else ""),
            )

    def _winlink_connect(self):
        host = self.config_data.get("pi_host", "192.168.1.229")
        port = self.config_data.get("port_winlink", 8080)
        self.wl_connect_btn.config(state="disabled", text="Relevé en cours…")

        def worker():
            try:
                res = api_client.pat_connect(host, port, "telnet")
                err = None
            except Exception as exc:
                res, err = None, str(exc)
            self.after(0, done, res, err)

        def done(res, err):
            self.wl_connect_btn.config(state="normal", text="Relever le courrier (Internet)")
            if err:
                messagebox.showerror(
                    "Winlink",
                    "Le relevé a échoué.\n\nVérifiez dans Pat (navigateur → Settings) que l'indicatif et le mot de "
                    f"passe Winlink sont renseignés, et que le Pi a accès à Internet.\n\nDétail : {err}",
                )
            else:
                n = (res or {}).get("NumReceived", 0)
                messagebox.showinfo("Winlink", f"Relevé terminé : {n} nouveau(x) message(s).")
            threading.Thread(target=self._fetch_winlink, daemon=True).start()

        threading.Thread(target=worker, daemon=True).start()

    def _winlink_open_message(self, box):
        tree = self.wl_trees[box]
        sel = tree.selection()
        if not sel:
            return
        mid = sel[0]
        host = self.config_data.get("pi_host", "192.168.1.229")
        port = self.config_data.get("port_winlink", 8080)

        def worker():
            try:
                msg = api_client.get_pat_message(host, port, box, mid)
                if box == "in":
                    try:
                        api_client.pat_mark_read(host, port, box, mid)
                    except Exception:
                        pass
                err = None
            except Exception as exc:
                msg, err = None, str(exc)
            self.after(0, show, msg, err)

        def show(msg, err):
            if err:
                messagebox.showerror("Winlink", f"Impossible d'ouvrir le message : {err}")
                return
            win = tk.Toplevel(self)
            win.title(msg.get("Subject") or "Message Winlink")
            win.configure(bg=theme.BG_CARD)
            win.geometry("720x520")
            to = ", ".join(self._wl_addr(a) for a in (msg.get("To") or []))
            hdr = (f"De : {self._wl_addr(msg.get('From'))}\nÀ : {to}\nDate : {self._wl_date(msg.get('Date'))}\n"
                   f"Objet : {msg.get('Subject') or ''}")
            files = msg.get("Files") or []
            if files:
                hdr += "\nPièces jointes : " + ", ".join(str(f.get("Name", f)) if isinstance(f, dict) else str(f) for f in files)
            tk.Label(win, text=hdr, bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY, justify="left",
                     anchor="w").pack(fill="x", padx=14, pady=(12, 8))
            txt = tk.Text(win, bg=theme.BG_CARD_ALT, fg=theme.TEXT, insertbackground=theme.TEXT, wrap="word",
                          font=("Consolas", 10), relief="flat", padx=10, pady=10)
            txt.insert("1.0", msg.get("Body") or "")
            txt.config(state="disabled")
            txt.pack(fill="both", expand=True, padx=14, pady=(0, 14))
            threading.Thread(target=self._fetch_winlink, daemon=True).start()

        threading.Thread(target=worker, daemon=True).start()

    # --------------------------------------------------------- Vigilance --

    VIGI_COLORS = {1: ("#3fb950", "VERT"), 2: ("#eab308", "JAUNE"), 3: ("#f97316", "ORANGE"), 4: ("#f85149", "ROUGE")}

    def _build_vigilance_card(self, parent):
        outer = theme.card(parent)
        outer.pack(fill="x", pady=(10, 0))
        f = outer.inner
        f.configure(padding=12)
        tk.Label(f, text="Vigilance Météo-France · Alpes-Maritimes (06)", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_SECTION).pack(anchor="w")
        self.vigi_rows = tk.Frame(f, bg=theme.BG_CARD)
        self.vigi_rows.pack(anchor="w", fill="x", pady=(8, 0))
        self.vigi_status = tk.Label(f, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", 8),
                                    wraplength=560, justify="left")
        self.vigi_status.pack(anchor="w", pady=(6, 0))
        key_row = tk.Frame(f, bg=theme.BG_CARD)
        self.vigi_key_row = key_row
        tk.Label(key_row, text="Clé API :", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SUBTITLE).pack(side="left")
        self.vigi_key_var = tk.StringVar()
        ttk.Entry(key_row, textvariable=self.vigi_key_var, width=28, show="•").pack(side="left", padx=6)
        ttk.Button(key_row, text="Enregistrer", style="Flat.TButton", command=self._vigi_save_key).pack(side="left")
        ttk.Button(key_row, text="Créer une clé (gratuit)", style="Flat.TButton",
                   command=lambda: self._open_url("https://portail-api.meteofrance.fr/web/fr/")).pack(side="left", padx=(6, 0))
        self._vigi_key_shown = False

    def _fetch_vigilance(self):
        host = self.config_data.get("pi_host", "192.168.1.229")
        port = self.config_data.get("port_monitor", 5008)
        try:
            data = api_client.get_vigilance(host, port)
        except Exception as exc:
            self.after(0, self._render_vigilance, None, str(exc))
            return
        self.after(0, self._render_vigilance, data, None)

    def _render_vigilance(self, data, error):
        if not hasattr(self, "vigi_rows"):
            return
        for w in self.vigi_rows.winfo_children():
            w.destroy()
        show_key = False
        if error:
            self.vigi_status.config(text="Service de surveillance du Pi injoignable (port 5008).", fg=theme.MUTED)
        elif data.get("status") == "non configurée":
            self.vigi_status.config(
                text=("Pour afficher la vigilance : créez un compte gratuit sur le portail API de Météo-France, "
                      "abonnez une application à l'API « Bulletin Vigilance », générez sa clé (onglet API Key, "
                      "durée 0) et collez-la ci-dessous. Elle est enregistrée sur le Pi."), fg=theme.MUTED)
            show_key = True
        elif data.get("status") != "ok":
            self.vigi_status.config(text=f"Vigilance indisponible ({data.get('status')}).", fg=theme.ORANGE)
            show_key = "clé" in str(data.get("status"))
        else:
            periods = data.get("periods") or []
            worst = 1
            for per in periods:
                color, name = self.VIGI_COLORS.get(per.get("max_color"), ("#8b96a5", "?"))
                worst = max(worst, per.get("max_color") or 1)
                row = tk.Frame(self.vigi_rows, bg=theme.BG_CARD)
                row.pack(anchor="w", fill="x", pady=2)
                tk.Label(row, text="Aujourd'hui" if per.get("echeance") == "J" else "Demain", width=11, anchor="w",
                         bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY_BOLD).pack(side="left")
                tk.Label(row, text=f"  {name}  ", bg=color, fg="#0b0f17", font=theme.FONT_CHIP).pack(side="left")
                phen = ", ".join(f"{p['name']} ({p['color_name']})" for p in per.get("phenomena") or [])
                tk.Label(row, text=phen or "pas de vigilance particulière", bg=theme.BG_CARD, fg=theme.MUTED,
                         font=theme.FONT_SUBTITLE, wraplength=300, justify="left").pack(side="left", padx=(8, 0))
            fetched = data.get("fetched_at")
            self.vigi_status.config(
                text=f"Mise à jour Météo-France : {time.strftime('%H:%M', time.localtime(fetched))}" if fetched else "",
                fg=theme.MUTED,
            )
            if "weather" in self.tile_chips and periods:
                cur = self.tile_chips["weather"].text.split("  ·  vigilance")[0]
                if worst >= 2:
                    self.tile_chips["weather"].config(text=f"{cur}  ·  vigilance {self.VIGI_COLORS[worst][1].lower()}")
            if worst >= 3:
                per = next(p for p in periods if p.get("max_color") == worst)
                phen = ", ".join(p["name"] for p in per.get("phenomena") or [] if p["color"] >= 3)
                day = "aujourd'hui" if per.get("echeance") == "J" else "demain"
                self._raise_alert(
                    f"vigi:{per.get('echeance')}:{per.get('begin')}:{worst}", f"Vigilance {self.VIGI_COLORS[worst][1]} — 06",
                    f"Météo-France : vigilance {self.VIGI_COLORS[worst][1].lower()} "
                    f"{day} — {phen}.", cooldown=12 * 3600,
                )
        if show_key and not self._vigi_key_shown:
            self.vigi_key_row.pack(anchor="w", pady=(8, 0))
            self._vigi_key_shown = True
        elif not show_key and self._vigi_key_shown:
            self.vigi_key_row.pack_forget()
            self._vigi_key_shown = False

    def _vigi_save_key(self):
        key = self.vigi_key_var.get().strip()
        if not key:
            return
        host, port = self._monitor_endpoint()

        def worker():
            try:
                api_client.set_monitor_config(host, port, {"vigilance": {"api_key": key}})
                msg, col = "Clé enregistrée sur le Pi : la vigilance s'affichera d'ici une minute.", theme.GREEN
            except Exception as exc:
                msg, col = f"Échec : {exc}", theme.RED
            self.after(0, lambda: (self.vigi_status.config(text=msg, fg=col), self.vigi_key_var.set("")))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------- Statistiques --

    def _build_stats_tab(self, frame):
        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)
        bar = tk.Frame(wrapper, bg=theme.BG)
        bar.pack(fill="x", padx=12, pady=(6, 6))
        tk.Label(bar, text="Période :", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_BODY).pack(side="left")
        self.stats_hours = 24
        self.stats_period_buttons = {}
        for hours, txt in ((6, "6 h"), (24, "24 h"), (24 * 7, "7 jours"), (24 * 30, "30 jours")):
            b = tk.Button(bar, text=txt, font=theme.FONT_BODY_BOLD, relief="flat", bd=0, padx=12, pady=4,
                          bg=theme.BG_CARD_ALT, fg=theme.TEXT, cursor="hand2",
                          command=lambda h=hours: self._stats_set_period(h))
            b.pack(side="left", padx=4)
            self.stats_period_buttons[hours] = b
        self.stats_status = tk.Label(bar, text="", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.stats_status.pack(side="left", padx=(16, 0))
        ttk.Button(bar, text="Actualiser", style="Flat.TButton", command=self._fetch_stats_async).pack(side="right")

        body = tk.Frame(wrapper, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        charts = tk.Frame(body, bg=theme.BG)
        charts.pack(side="left", fill="both", expand=True)
        specs = (
            ("aircraft", "Avions suivis", "", "#ff7a1a"),
            ("adsb_max_km", "Portée ADS-B max", "km", "#f0b429"),
            ("ships", "Navires suivis", "", "#3b82f6"),
            ("ais_max_km", "Portée AIS max", "km", "#06b6d4"),
            ("aprs_stations", "Stations APRS (30 dernières min)", "", "#22c55e"),
            ("temp", "Température CPU (rouge = sous-tension)", "°C", "#f472b6"),
        )
        self.stats_charts = {}
        for i, (key, title, unit, color) in enumerate(specs):
            r, c = divmod(i, 2)
            charts.grid_columnconfigure(c, weight=1, uniform="ch")
            charts.grid_rowconfigure(r, weight=1)
            outer = theme.card(charts)
            outer.grid(row=r, column=c, sticky="nsew", padx=4, pady=4)
            ch = _LineChart(outer.inner, title, unit, color, height=180)
            ch.pack(fill="both", expand=True, padx=6, pady=6)
            self.stats_charts[key] = ch

        side = tk.Frame(body, bg=theme.BG)
        side.pack(side="left", fill="y", padx=(8, 0))
        self.stats_polars = {}
        for kind, title, color in (("adsb", "Polaire de portée ADS-B", "#f0b429"), ("ais", "Polaire de portée AIS", "#06b6d4")):
            outer = theme.card(side)
            outer.pack(fill="x", pady=4)
            p = _PolarChart(outer.inner, title, color, size=300)
            p.pack(padx=6, pady=6)
            self.stats_polars[kind] = p
        self.stats_share = tk.Label(side, text="", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE,
                                    justify="left", wraplength=320)
        self.stats_share.pack(anchor="w", pady=(6, 0))
        self._stats_poll_running = False
        self._stats_set_period(24, fetch=False)

    def _stats_set_period(self, hours, fetch=True):
        self.stats_hours = hours
        for h, b in self.stats_period_buttons.items():
            b.config(bg=theme.ACCENT if h == hours else theme.BG_CARD_ALT,
                     fg="#1a1200" if h == hours else theme.TEXT)
        if fetch:
            self._fetch_stats_async()

    def _stats_poll(self):
        if getattr(self, "_current_view", None) != "stats":
            self._stats_poll_running = False
            return
        self._fetch_stats_async()
        self.after(60000, self._stats_poll)

    def _fetch_stats_async(self):
        host = self.config_data.get("pi_host", "192.168.1.229")
        port = self.config_data.get("port_monitor", 5008)
        hours = self.stats_hours
        self.stats_status.config(text="Chargement…", fg=theme.MUTED)

        def worker():
            try:
                data = {
                    "series": api_client.get_stats_series(host, port, hours),
                    "adsb": api_client.get_stats_polar(host, port, "adsb", hours),
                    "ais": api_client.get_stats_polar(host, port, "ais", hours),
                }
                err = None
            except Exception as exc:
                data, err = None, str(exc)
            self.after(0, self._render_stats, data, err)

        threading.Thread(target=worker, daemon=True).start()

    def _render_stats(self, data, error):
        if error:
            self.stats_status.config(
                text="Statistiques indisponibles — le service de surveillance (port 5008) est-il installé ?",
                fg=theme.RED,
            )
            return
        ser = data["series"]
        cols, rows = ser["columns"], ser["rows"]
        idx = {c: i for i, c in enumerate(cols)}
        t1 = time.time()
        t0 = t1 - ser.get("hours", self.stats_hours) * 3600
        notes = {"aircraft": "ADS-B n'a pas eu la clé sur la période", "adsb_max_km": "ADS-B n'a pas eu la clé",
                 "ships": "AIS n'a pas eu la clé sur la période", "ais_max_km": "AIS n'a pas eu la clé",
                 "aprs_stations": "APRS n'a pas eu la clé sur la période"}
        bands, start = [], None
        for r in rows:
            uv = r[idx["undervolt"]]
            if uv and start is None:
                start = r[0]
            elif not uv and start is not None:
                bands.append((start, r[0]))
                start = None
        if start is not None:
            bands.append((start, t1))
        for key, ch in self.stats_charts.items():
            pts = [(r[0], r[idx[key]]) for r in rows]
            ch.set(pts, t0, t1, bands=bands if key == "temp" else None, note=notes.get(key, ""))
        self.stats_polars["adsb"].set(data["adsb"].get("max_km"))
        self.stats_polars["ais"].set(data["ais"].get("max_km"))
        # Répartition du temps de clé
        modes = {}
        for r in rows:
            m = r[idx["mode"]] or "?"
            modes[m] = modes.get(m, 0) + 1
        total = sum(modes.values())
        if total:
            share = "  ·  ".join(f"{SDR_MODE_LABELS.get(m, m)} {n * 100 / total:.0f} %" for m, n in
                                 sorted(modes.items(), key=lambda x: -x[1]))
            uv_pct = sum(1 for r in rows if r[idx["undervolt"]]) * 100 / total
            self.stats_share.config(text=f"Temps de clé : {share}\nSous-tension : {uv_pct:.0f} % du temps")
        else:
            self.stats_share.config(text="Pas encore de données : le service relève une mesure par minute.")
        self.stats_status.config(text=f"{len(rows)} mesure(s) · mis à jour à {time.strftime('%H:%M:%S')}",
                                 fg=theme.MUTED)
        if "stats" in self.tile_chips:
            vals = [r[idx["adsb_max_km"]] for r in rows if r[idx["adsb_max_km"]]]
            self.tile_chips["stats"].config(text=f"Portée ADS-B max {max(vals):.0f} km" if vals else "Historique 30 jours")

    # ------------------------------------------------------- Notifications --

    def _build_notify_card(self, parent):
        outer = theme.card(parent)
        outer.pack(fill="x", padx=12, pady=(0, 8))
        f = outer.inner
        f.configure(padding=14)
        tk.Label(f, text="Notifications sur téléphone / e-mail (même PC éteint)", bg=theme.BG_CARD,
                 fg=theme.TEXT, font=theme.FONT_SECTION).grid(row=0, column=0, columnspan=8, sticky="w")
        tk.Label(f, text=(
            "Téléphone : installez l'appli gratuite « ntfy » (Android / iPhone), abonnez-vous au sujet indiqué "
            "ci-dessous (choisissez un nom difficile à deviner, il fait office de mot de passe)."),
            bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE, justify="left", wraplength=1300,
        ).grid(row=1, column=0, columnspan=8, sticky="w", pady=(4, 8))

        def lab(r, c, t):
            tk.Label(f, text=t, bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY).grid(row=r, column=c, sticky="e", padx=(12, 6), pady=3)

        def chk(r, c, var, t):
            tk.Checkbutton(f, text=t, variable=var, bg=theme.BG_CARD, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT,
                           activebackground=theme.BG_CARD, activeforeground=theme.TEXT, font=theme.FONT_BODY,
                           highlightthickness=0).grid(row=r, column=c, sticky="w", pady=3)

        self.nt_vars = {k: tk.StringVar() for k in ("topic", "server", "smtp_host", "smtp_port", "user", "password", "to")}
        self.nt_ntfy_on, self.nt_mail_on = tk.BooleanVar(), tk.BooleanVar()
        chk(2, 0, self.nt_ntfy_on, "Téléphone (ntfy)")
        lab(2, 1, "Sujet :")
        ttk.Entry(f, textvariable=self.nt_vars["topic"], width=26).grid(row=2, column=2, sticky="w")
        lab(2, 3, "Serveur :")
        ttk.Entry(f, textvariable=self.nt_vars["server"], width=24).grid(row=2, column=4, sticky="w")
        chk(3, 0, self.nt_mail_on, "E-mail")
        lab(3, 1, "Serveur SMTP :")
        ttk.Entry(f, textvariable=self.nt_vars["smtp_host"], width=26).grid(row=3, column=2, sticky="w")
        lab(3, 3, "Port :")
        ttk.Entry(f, textvariable=self.nt_vars["smtp_port"], width=6).grid(row=3, column=4, sticky="w")
        lab(4, 1, "Identifiant :")
        ttk.Entry(f, textvariable=self.nt_vars["user"], width=26).grid(row=4, column=2, sticky="w")
        lab(4, 3, "Mot de passe :")
        ttk.Entry(f, textvariable=self.nt_vars["password"], width=24, show="•").grid(row=4, column=4, sticky="w")
        self.nt_pw_hint = tk.Label(f, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.nt_pw_hint.grid(row=4, column=5, columnspan=3, sticky="w", padx=(8, 0))
        lab(5, 1, "Destinataire :")
        ttk.Entry(f, textvariable=self.nt_vars["to"], width=26).grid(row=5, column=2, sticky="w")

        tk.Label(f, text="Événements à envoyer :", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_BODY).grid(row=6, column=0, sticky="w", pady=(10, 2))
        self.nt_events = {}
        ev = tk.Frame(f, bg=theme.BG_CARD)
        ev.grid(row=7, column=0, columnspan=8, sticky="w")
        labels = {
            "squawk": "Squawk 7700/7600/7500", "sart": "Balises AIS SART/EPIRB/MOB", "aprs_msg": "Messages APRS",
            "winlink": "Courrier Winlink", "undervoltage": "Sous-tension du Pi", "dongle": "Clé SDR disparue",
            "vigilance": "Vigilance orange/rouge", "beacon": "Balise 406 réelle", "beacon_test": "Balise 406 test",
        }
        for i, (k, t) in enumerate(labels.items()):
            v = tk.BooleanVar(value=True)
            self.nt_events[k] = v
            tk.Checkbutton(ev, text=t, variable=v, bg=theme.BG_CARD, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT,
                           activebackground=theme.BG_CARD, activeforeground=theme.TEXT, font=theme.FONT_BODY,
                           highlightthickness=0).grid(row=i // 5, column=i % 5, sticky="w", padx=(0, 14))
        rep_row = tk.Frame(f, bg=theme.BG_CARD)
        rep_row.grid(row=8, column=0, columnspan=8, sticky="w", pady=(10, 0))
        self.nt_report_on = tk.BooleanVar(value=False)
        tk.Checkbutton(rep_row, text="Rapport quotidien (résumé de la journée) à", variable=self.nt_report_on,
                       bg=theme.BG_CARD, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG_CARD,
                       activeforeground=theme.TEXT, font=theme.FONT_BODY, highlightthickness=0).pack(side="left")
        self.nt_report_hour = tk.StringVar(value="20")
        ttk.Spinbox(rep_row, from_=0, to=23, width=4, textvariable=self.nt_report_hour).pack(side="left", padx=4)
        tk.Label(rep_row, text="h", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY).pack(side="left")
        ttk.Button(rep_row, text="Aperçu", style="Flat.TButton", command=self._report_preview).pack(side="left", padx=(16, 0))
        ttk.Button(rep_row, text="Envoyer le rapport maintenant", style="Flat.TButton",
                   command=self._report_send).pack(side="left", padx=(6, 0))
        btns = tk.Frame(f, bg=theme.BG_CARD)
        btns.grid(row=9, column=0, columnspan=8, sticky="w", pady=(10, 0))
        ttk.Button(btns, text="Enregistrer", style="Accent.TButton", command=self._notify_save).pack(side="left")
        ttk.Button(btns, text="Envoyer un test", style="Flat.TButton", command=self._notify_test).pack(side="left", padx=8)
        self.nt_status = tk.Label(btns, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.nt_status.pack(side="left", padx=(8, 0))
        self._notify_loaded = False

    def _monitor_endpoint(self):
        return self.config_data.get("pi_host", "192.168.1.229"), self.config_data.get("port_monitor", 5008)

    def _notify_load(self):
        host, port = self._monitor_endpoint()

        def worker():
            try:
                cfg, err = api_client.get_monitor_config(host, port), None
            except Exception as exc:
                cfg, err = None, str(exc)
            self.after(0, done, cfg, err)

        def done(cfg, err):
            if err:
                self.nt_status.config(text="Service de surveillance injoignable (port 5008) — installez-le sur le Pi.",
                                      fg=theme.RED)
                return
            n, e = cfg.get("ntfy", {}), cfg.get("email", {})
            self.nt_ntfy_on.set(bool(n.get("enabled")))
            self.nt_mail_on.set(bool(e.get("enabled")))
            self.nt_vars["topic"].set(n.get("topic", ""))
            self.nt_vars["server"].set(n.get("server", "https://ntfy.sh"))
            for k in ("smtp_host", "user", "to"):
                self.nt_vars[k].set(e.get(k, ""))
            self.nt_vars["smtp_port"].set(str(e.get("smtp_port", 465)))
            self.nt_vars["password"].set("")
            self.nt_pw_hint.config(text="(enregistré sur le Pi — laisser vide pour le garder)" if e.get("password_set")
                                   else "(non renseigné)")
            for k, v in self.nt_events.items():
                v.set(bool(cfg.get("events", {}).get(k, True)))
            dr = cfg.get("daily_report") or {}
            self.nt_report_on.set(bool(dr.get("enabled")))
            self.nt_report_hour.set(str(dr.get("hour", 20)))
            self._notify_loaded = True
            self.nt_status.config(text="Réglages chargés depuis le Pi.", fg=theme.MUTED)

        threading.Thread(target=worker, daemon=True).start()

    def _notify_save(self, then=None):
        try:
            port_smtp = int(self.nt_vars["smtp_port"].get() or 465)
        except ValueError:
            port_smtp = 465
        payload = {
            "ntfy": {"enabled": self.nt_ntfy_on.get(), "topic": self.nt_vars["topic"].get().strip(),
                     "server": self.nt_vars["server"].get().strip() or "https://ntfy.sh"},
            "email": {"enabled": self.nt_mail_on.get(), "smtp_host": self.nt_vars["smtp_host"].get().strip(),
                      "smtp_port": port_smtp, "ssl": port_smtp == 465, "user": self.nt_vars["user"].get().strip(),
                      "password": self.nt_vars["password"].get(), "to": self.nt_vars["to"].get().strip()},
            "events": {k: v.get() for k, v in self.nt_events.items()},
        }
        try:
            payload["daily_report"] = {"enabled": bool(self.nt_report_on.get()),
                                       "hour": max(0, min(23, int(self.nt_report_hour.get())))}
        except ValueError:
            payload["daily_report"] = {"enabled": bool(self.nt_report_on.get()), "hour": 20}
        host, port = self._monitor_endpoint()
        self.nt_status.config(text="Enregistrement…", fg=theme.MUTED)

        def worker():
            try:
                api_client.set_monitor_config(host, port, payload)
                err = None
            except Exception as exc:
                err = str(exc)
            self.after(0, done, err)

        def done(err):
            if err:
                self.nt_status.config(text=f"Échec : {err}", fg=theme.RED)
                return
            self.nt_status.config(text=f"Enregistré sur le Pi à {time.strftime('%H:%M:%S')}.", fg=theme.GREEN)
            self._notify_load()
            if then:
                then()

        threading.Thread(target=worker, daemon=True).start()

    def _report_preview(self):
        host, port = self._monitor_endpoint()

        def worker():
            try:
                r = requests.get(f"http://{host}:{port}/api/monitor/report", timeout=15)
                r.raise_for_status()
                d = r.json()
                self.after(0, lambda: messagebox.showinfo(d.get("title", "Rapport"), d.get("body", "")))
            except Exception as exc:
                self.after(0, lambda: self.nt_status.config(text=f"Aperçu impossible : {exc}", fg=theme.RED))

        threading.Thread(target=worker, daemon=True).start()

    def _report_send(self):
        host, port = self._monitor_endpoint()

        def send():
            def worker():
                try:
                    r = requests.post(f"http://{host}:{port}/api/monitor/report", timeout=40)
                    r.raise_for_status()
                    ch = r.json().get("channels") or {}
                    txt = ("Rapport envoyé : " + ", ".join(f"{k} {v}" for k, v in ch.items())) if ch else \
                        "Aucun canal configuré (téléphone ou e-mail)."
                    col = theme.GREEN if ch and all(v == "ok" for v in ch.values()) else theme.ORANGE
                except Exception as exc:
                    txt, col = f"Échec : {exc}", theme.RED
                self.after(0, lambda: self.nt_status.config(text=txt, fg=col))
            threading.Thread(target=worker, daemon=True).start()

        self._notify_save(then=send)

    def _notify_test(self):
        host, port = self._monitor_endpoint()

        def send():
            self.nt_status.config(text="Envoi du test…", fg=theme.MUTED)

            def worker():
                try:
                    res, err = api_client.monitor_test(host, port), None
                except Exception as exc:
                    res, err = None, str(exc)
                self.after(0, done, res, err)

            threading.Thread(target=worker, daemon=True).start()

        def done(res, err):
            if err:
                self.nt_status.config(text=f"Échec : {err}", fg=theme.RED)
                return
            ch = (res or {}).get("channels") or {}
            if not ch:
                self.nt_status.config(text="Aucun canal configuré : indiquez un sujet ntfy ou un e-mail.", fg=theme.ORANGE)
                return
            names = {"ntfy": "Téléphone", "email": "E-mail"}
            txt = "   ·   ".join(f"{names.get(k, k)} : {v}" for k, v in ch.items())
            ok = all(v == "ok" for v in ch.values())
            self.nt_status.config(text=txt, fg=theme.GREEN if ok else theme.RED)

        self._notify_save(then=send)

    # ------------------------------------------------- Balises de détresse --

    @staticmethod
    def _compass(deg):
        if deg is None:
            return ""
        names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO"]
        return names[int((deg % 360) / 22.5 + 0.5) % 16]

    def _build_b406_panel(self, frame):
        outer = tk.Frame(frame, bg=theme.BG)
        outer.pack(fill="both", expand=True, padx=4, pady=4)
        left = tk.Frame(outer, bg=theme.BG, width=800)
        left.pack(side="left", fill="y", pady=(0, 6))
        left.pack_propagate(False)

        st_outer = theme.card(left)
        st_outer.pack(fill="x", padx=12, pady=(8, 6))
        st = st_outer.inner
        st.configure(padding=12)
        row = tk.Frame(st, bg=theme.BG_CARD)
        row.pack(fill="x")
        tk.Label(row, text="Balises de détresse 406 MHz", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_SECTION).pack(side="left")
        self.b406_pill = theme.pill(row, "INCONNU", theme.MUTED)
        self.b406_pill.pack(side="left", padx=(12, 0))
        ttk.Button(row, text="Effacer l'historique", style="Flat.TButton", command=self._beacon_clear).pack(side="right")
        ttk.Button(row, text="Écouter le 406 MHz", style="Accent.TButton",
                   command=lambda: self._switch_sdr("beacon")).pack(side="right", padx=(0, 8))
        self.b406_info = tk.Label(st, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE,
                                  justify="left", anchor="w", wraplength=760)
        self.b406_info.pack(anchor="w", pady=(6, 0))

        tk.Label(left, text="Balises entendues (une ligne par balise)", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION).pack(
            anchor="w", padx=12, pady=(2, 4))
        tbl = theme.card(left)
        tbl.pack(fill="x", padx=12, pady=(0, 6))
        cols = ("time", "mode", "hex", "proto", "country", "count", "dist")
        self.b406_tree = ttk.Treeview(tbl.inner, columns=cols, show="headings", height=6)
        for c, (t, w, a) in {"time": ("Dernière réception", 125, "center"), "mode": ("Mode", 78, "center"),
                             "hex": ("Hex ID", 178, "w"), "proto": ("Protocole", 150, "w"),
                             "country": ("Pays", 72, "w"), "count": ("Reçue", 48, "center"),
                             "dist": ("Distance / relèvement", 120, "e")}.items():
            self.b406_tree.heading(c, text=t)
            self.b406_tree.column(c, width=w, minwidth=40, anchor=a)
        self.b406_tree.tag_configure("real", background="#5a1f22", foreground="#ffffff")
        self.b406_tree.tag_configure("test", foreground="#e3b341")
        vsb = ttk.Scrollbar(tbl.inner, orient="vertical", command=self.b406_tree.yview)
        self.b406_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.b406_tree.pack(fill="x", padx=1, pady=1)
        self.b406_tree.bind("<<TreeviewSelect>>", self._on_beacon_select)

        man = tk.Frame(left, bg=theme.BG)
        man.pack(fill="x", padx=12, pady=(0, 6))
        tk.Label(man, text="Décoder une trame (hexadécimal) :", bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_SUBTITLE).pack(side="left")
        self.b406_hex_var = tk.StringVar()
        ttk.Entry(man, textvariable=self.b406_hex_var, width=46).pack(side="left", padx=6)
        ttk.Button(man, text="Décoder", style="Accent.TButton", command=self._beacon_manual_decode).pack(side="left")

        det = theme.card(left)
        det.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        d = det.inner
        d.configure(padding=10)
        self.b406_title = tk.Label(d, text="Sélectionnez une trame pour voir son décodage complet.", bg=theme.BG_CARD,
                                   fg=theme.TEXT, font=("Segoe UI", 13, "bold"), anchor="w", justify="left",
                                   wraplength=760)
        self.b406_title.pack(anchor="w")
        self.b406_where = tk.Label(d, text="", bg=theme.BG_CARD, fg=theme.ACCENT, font=theme.FONT_BODY_BOLD,
                                   anchor="w", justify="left", wraplength=760)
        self.b406_where.pack(anchor="w", pady=(4, 0))
        btns = tk.Frame(d, bg=theme.BG_CARD)
        btns.pack(anchor="w", pady=(6, 6))
        self.b406_osm_btn = ttk.Button(btns, text="Ouvrir la position (OpenStreetMap)", style="Flat.TButton",
                                       command=lambda: self._beacon_open_map("osm"), state="disabled")
        self.b406_osm_btn.pack(side="left")
        self.b406_geo_btn = ttk.Button(btns, text="Géoportail (IGN)", style="Flat.TButton",
                                       command=lambda: self._beacon_open_map("ign"), state="disabled")
        self.b406_geo_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="Copier le décodage", style="Flat.TButton", command=self._beacon_copy).pack(side="left")
        self.b406_text = tk.Text(d, bg=theme.BG_CARD_ALT, fg=theme.TEXT, font=("Consolas", 9), relief="flat",
                                 wrap="word", height=12, padx=8, pady=6)
        self.b406_text.pack(fill="both", expand=True)
        self.b406_text.config(state="disabled")

        right = tk.Frame(outer, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True, pady=(8, 12), padx=(0, 12))
        map_outer = theme.card(right)
        map_outer.pack(fill="both", expand=True)
        self.b406_map = self._make_map(map_outer.inner)
        self.b406_map.pack(fill="both", expand=True, padx=1, pady=1)
        self.b406_map.set_position(*DEFAULT_COORDS)
        self.b406_map.set_zoom(7)
        tk.Label(right, text="Activité sur 406,0–406,1 MHz (salves détectées)", bg=theme.BG, fg=theme.TEXT,
                 font=theme.FONT_BODY_BOLD).pack(anchor="w", pady=(8, 2))
        bt = theme.card(right)
        bt.pack(fill="x")
        bcols = ("time", "freq", "type", "snr", "status")
        self.b406_bursts = ttk.Treeview(bt.inner, columns=bcols, show="headings", height=5)
        for c, (t, w) in {"time": ("Heure", 90), "freq": ("Fréquence (MHz)", 120), "type": ("Type", 70),
                          "snr": ("SNR (dB)", 80), "status": ("Résultat", 140)}.items():
            self.b406_bursts.heading(c, text=t)
            self.b406_bursts.column(c, width=w, anchor="center")
        self.b406_bursts.pack(fill="x", padx=1, pady=1)
        tk.Label(right, text=(
            "Écoute seule. Une balise émet une salve d'environ 0,5 s toutes les 50 s. Trame RÉELLE (hors mode "
            "test) : une alerte de détresse est peut-être en cours — les secours sont normalement déjà alertés "
            "par satellite ; en cas de doute, prévenir le CROSS (196) ou le 112."),
            bg=theme.BG, fg=theme.MUTED, font=("Segoe UI", 8), justify="left", wraplength=900,
        ).pack(anchor="w", pady=(6, 0))

        self._b406_frames = []
        self._b406_selected = None
        self._b406_poll_running = False
        self._b406_markers = []

    def _beacon_endpoint(self):
        return self.config_data.get("pi_host", "192.168.1.229"), self.config_data.get("port_beacon", 5009)

    def _beacon_poll(self):
        if getattr(self, "_current_view", None) != "beacon":
            self._b406_poll_running = False
            return
        threading.Thread(target=self._fetch_beacon, daemon=True).start()
        self.after(5000, self._beacon_poll)

    def _fetch_beacon(self, frames_only=False):
        host, port = self._beacon_endpoint()
        try:
            data = {"frames": api_client.get_beacon(host, port, "frames")}
            if not frames_only:
                data["status"] = api_client.get_beacon(host, port, "status")
                data["bursts"] = api_client.get_beacon(host, port, "bursts")
        except Exception as exc:
            self.after(0, self._render_beacon, None, str(exc))
            return
        self.after(0, self._render_beacon, data, None)

    def _render_beacon(self, data, error):
        if not hasattr(self, "b406_tree"):
            return
        mode = getattr(self, "_sdr_mode", None)
        if error:
            sw = getattr(self, "_sdr_switch_error", None)
            if sw and sw[0] == "beacon":
                self.b406_pill.config(text="  BASCULE IMPOSSIBLE  ", bg=theme.RED)
                self.b406_info.config(text=f"La clé n'a pas pu passer sur le 406 MHz : {sw[1]}", fg=theme.ORANGE)
            elif mode not in (None, "beacon"):
                txt = "EN PAUSE" if mode != "off" else "CLÉ ARRÊTÉE"
                self.b406_pill.config(text=f"  {txt}  ", bg=theme.MUTED)
                self.b406_info.config(text=(f"La clé SDR est sur {SDR_MODE_LABELS.get(mode, mode)}. Cliquez sur "
                                            "« Écouter le 406 MHz » pour lui donner la clé."), fg=theme.MUTED)
            else:
                self.b406_pill.config(text="  NON INSTALLÉ / INJOIGNABLE  ", bg=theme.MUTED)
                self.b406_info.config(text="Le service beacon406 (port 5009) ne répond pas — installez-le sur le Pi "
                                           "(install_beacon406.sh).")
            if "beacon" in self.tile_chips and mode != "elt121":
                self.tile_chips["beacon"].config(text="—")
            return
        frames = data.get("frames") or []
        self._b406_frames = frames
        self._check_beacon_alerts(frames)
        st = data.get("status")
        if st is not None:
            if st.get("running"):
                self.b406_pill.config(text="  ÉCOUTE  ", bg=theme.GREEN)
            else:
                self.b406_pill.config(text="  ARRÊTÉ  ", bg=theme.RED)
            lb = st.get("last_burst")
            last = (f"dernière salve {time.strftime('%H:%M:%S', time.localtime(lb['time']))} sur {lb['freq_mhz']:.4f} MHz "
                    f"({lb['type']}, SNR {lb['snr_db']:.0f} dB, {lb.get('status', '')})") if lb else "aucune salve détectée"
            err = f"\n⚠ {st['error']}" if st.get("error") else ""
            self.b406_info.config(text=(
                f"Bande {st.get('band', '406,0–406,1 MHz')}  ·  {st.get('bursts_last_hour', 0)} salve(s) la dernière heure  ·  "
                f"{st.get('decoded', 0)} trame(s) décodée(s), {st.get('failed', 0)} non décodée(s) depuis le démarrage\n{last}{err}"),
                fg=theme.ORANGE if err else theme.MUTED)
            self.b406_bursts.delete(*self.b406_bursts.get_children())
            for b in (data.get("bursts") or [])[:30]:
                self.b406_bursts.insert("", "end", values=(
                    time.strftime("%H:%M:%S", time.localtime(b["time"])), f"{b['freq_mhz']:.4f}", b.get("type", ""),
                    f"{b.get('snr_db', 0):.0f}", b.get("status", "")))
        # Une ligne par balise (Hex ID + mode) : la plus récente trame, le
        # nombre de réceptions, et la dernière position connue.
        groups = {}
        for f in frames:  # du plus récent au plus ancien
            k = (f.get("hex_id"), bool(f.get("test")))
            g = groups.get(k)
            if g is None:
                groups[k] = g = {"latest": f, "count": 0, "pos": None}
            g["count"] += 1
            if g["pos"] is None and f.get("lat") is not None:
                g["pos"] = f
        self._b406_groups = list(groups.values())
        tree = self.b406_tree
        tree.delete(*tree.get_children())
        for i, g in enumerate(self._b406_groups):
            f, pf = g["latest"], g["pos"]
            dist = (f"{pf['distance_km']:.0f} km · {pf['bearing_deg']}° {self._compass(pf['bearing_deg'])}"
                    if pf is not None and pf.get("distance_km") is not None else "pas de position")
            tree.insert("", "end", iid=str(i), tags=("test",) if f.get("test") else ("real",), values=(
                time.strftime("%d/%m %H:%M:%S", time.localtime(f.get("time", 0))),
                f"{'TEST' if f.get('test') else 'RÉELLE'} {f.get('generation', '')}", f.get("hex_id") or "?",
                re.sub(r"^\d+\s*\((.*)\)$", r"\1", f.get("protocol") or ""),
                re.sub(r"^\d+\s*\(|\)$", "", f.get("country") or ""), g["count"], dist))
        # Carte
        for m in self._b406_markers:
            try:
                m.delete()
            except Exception:
                pass
        self._b406_markers = [self.b406_map.set_marker(*DEFAULT_COORDS, text="F4JCF", text_color="#16a34a",
                                                       marker_color_circle="#14532d", marker_color_outside="#22c55e")]
        self.b406_map.delete_all_path()
        seen = set()
        for f in frames:
            if f.get("lat") is None or f.get("hex_id") in seen:
                continue
            seen.add(f.get("hex_id"))
            real = not f.get("test")
            self._b406_markers.append(self.b406_map.set_marker(
                f["lat"], f["lon"], text=("⚠ " if real else "") + (f.get("hex_id") or "")[:8],
                text_color="#b91c1c" if real else "#a16207",
                marker_color_circle="#7f1d1d" if real else "#713f12",
                marker_color_outside="#ef4444" if real else "#f59e0b"))
        if "beacon" in self.tile_chips and mode != "elt121":
            real = sum(1 for f in frames if not f.get("test") and time.time() - f.get("time", 0) < 86400)
            self.tile_chips["beacon"].config(
                text=(f"⚠ {real} réelle(s) sur 24 h" if real else f"{len(frames)} trame(s)") if frames else "aucune trame")
        if self._b406_selected is not None:
            for i, g in enumerate(self._b406_groups):
                if (g["latest"].get("hex_id"), bool(g["latest"].get("test"))) == self._b406_selected:
                    tree.selection_set(str(i))
                    break

    def _check_beacon_alerts(self, frames):
        for f in frames:
            if f.get("manual") or time.time() - (f.get("time") or 0) > 3600:
                continue
            test = bool(f.get("test"))
            pos = ""
            if f.get("lat") is not None:
                pos = (f"\nPosition : {f['lat']:.5f}, {f['lon']:.5f} — {f.get('distance_km')} km au "
                       f"{f.get('bearing_deg')}° ({self._compass(f.get('bearing_deg'))})")
            self._raise_alert(
                f"b406:{f.get('hex_id')}:{test}",
                "Balise 406 — trame de TEST / exercice" if test else "BALISE 406 — ÉMISSION RÉELLE",
                f"Hex ID {f.get('hex_id')} · {f.get('protocol') or '?'} · {f.get('country') or '?'}\n"
                f"{f.get('identification') or ''}{pos}", cooldown=3600 if test else 600)

    def _on_beacon_select(self, _e=None):
        sel = self.b406_tree.selection()
        groups = getattr(self, "_b406_groups", [])
        if sel:
            idx = int(sel[0])
            if 0 <= idx < len(groups):
                g = groups[idx]
                f = dict(g["latest"])
                if f.get("lat") is None and g["pos"] is not None:  # garder la dernière position connue
                    for k in ("lat", "lon", "distance_km", "bearing_deg"):
                        f[k] = g["pos"].get(k)
                self._b406_selected = (f.get("hex_id"), bool(f.get("test")))
                self._show_beacon(f)

    def _show_beacon(self, f):
        self._b406_shown = f
        test = f.get("test")
        self.b406_title.config(
            text=f"{'TEST / EXERCICE' if test else 'ÉMISSION RÉELLE'} — {f.get('protocol') or 'balise'} — Hex ID {f.get('hex_id')}"
                 + ("  (décodage manuel)" if f.get("manual") else ""),
            fg=theme.ORANGE if test else theme.RED)
        if f.get("lat") is not None:
            self.b406_where.config(text=(
                f"Position : {f['lat']:.5f}, {f['lon']:.5f}   →   {f['distance_km']:.1f} km de la station, "
                f"relèvement {f['bearing_deg']}° ({self._compass(f['bearing_deg'])})"))
            self.b406_osm_btn.config(state="normal")
            self.b406_geo_btn.config(state="normal")
            self.b406_map.set_position(f["lat"], f["lon"])
            self.b406_map.delete_all_path()
            self.b406_map.set_path([DEFAULT_COORDS, (f["lat"], f["lon"])], color="#ef4444" if not test else "#f59e0b", width=2)
        else:
            self.b406_where.config(text="Pas de position dans la trame (balise sans GPS ou position non encore acquise) : "
                                        "localisation par radiogoniométrie sur 121,5 MHz.")
            self.b406_osm_btn.config(state="disabled")
            self.b406_geo_btn.config(state="disabled")
        extra = ""
        if f.get("freq_mhz"):
            extra = f"Reçue le {time.strftime('%d/%m/%Y à %H:%M:%S', time.localtime(f.get('time', 0)))} sur " \
                    f"{f['freq_mhz']:.4f} MHz, SNR {f.get('snr_db', 0):.0f} dB\n\n"
        self.b406_text.config(state="normal")
        self.b406_text.delete("1.0", "end")
        self.b406_text.insert("1.0", extra + (f.get("text") or ""))
        self.b406_text.config(state="disabled")

    def _beacon_open_map(self, kind):
        f = getattr(self, "_b406_shown", None)
        if not f or f.get("lat") is None:
            return
        if kind == "ign":
            url = f"https://www.geoportail.gouv.fr/carte?c={f['lon']},{f['lat']}&z=15&l0=GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2::GEOPORTAIL:OGC:WMTS(1)&permalink=yes"
        else:
            url = f"https://www.openstreetmap.org/?mlat={f['lat']}&mlon={f['lon']}#map=14/{f['lat']}/{f['lon']}"
        self._open_url(url)

    def _beacon_copy(self):
        f = getattr(self, "_b406_shown", None)
        if f:
            self.clipboard_clear()
            self.clipboard_append(f.get("text") or "")

    def _beacon_manual_decode(self):
        hx = re.sub(r"\s", "", self.b406_hex_var.get())
        if not hx:
            return
        host, port = self._beacon_endpoint()

        def worker():
            try:
                res = api_client.beacon_decode(host, port, hx)
            except Exception as exc:
                res = {"error": f"service injoignable : {exc}"}
            self.after(0, done, res)

        def done(res):
            if res.get("error"):
                messagebox.showwarning("Décodage", res["error"])
                return
            self._b406_selected = None
            self._show_beacon(res)

        threading.Thread(target=worker, daemon=True).start()

    def _beacon_clear(self):
        if not messagebox.askyesno("Balises 406", "Effacer l'historique des trames et des salves ?"):
            return
        host, port = self._beacon_endpoint()

        def worker():
            try:
                api_client.beacon_clear(host, port)
            except Exception:
                pass
            self._fetch_beacon()

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------- Capteurs 433 --

    S433_FIELDS = {
        "temperature_C": ("Température", "{:.1f} °C"), "temperature_F": ("Température", "{:.1f} °F"),
        "humidity": ("Humidité", "{:.0f} %"), "pressure_hPa": ("Pression", "{:.1f} hPa"),
        "wind_avg_km_h": ("Vent moyen", "{:.1f} km/h"), "wind_max_km_h": ("Rafales", "{:.1f} km/h"),
        "wind_avg_m_s": ("Vent moyen", "{:.1f} m/s"), "wind_max_m_s": ("Rafales", "{:.1f} m/s"),
        "wind_dir_deg": ("Direction du vent", "{:.0f}°"), "rain_mm": ("Pluie (cumul)", "{:.1f} mm"),
        "rain_rate_mm_h": ("Intensité de pluie", "{:.1f} mm/h"), "uv": ("Indice UV", "{}"),
        "light_lux": ("Luminosité", "{:.0f} lux"), "moisture": ("Humidité du sol", "{} %"),
        "power_W": ("Puissance", "{:.0f} W"), "energy_kWh": ("Énergie", "{:.2f} kWh"),
        "depth_cm": ("Profondeur", "{} cm"), "button": ("Bouton", "{}"), "state": ("État", "{}"),
        "event": ("Événement", "{}"), "cmd": ("Commande", "{}"), "code": ("Code", "{}"),
    }

    def _build_s433_tab(self, frame):
        outer = tk.Frame(frame, bg=theme.BG)
        outer.pack(fill="both", expand=True, padx=4, pady=4)
        head = theme.card(outer)
        head.pack(fill="x", padx=12, pady=(8, 6))
        h = head.inner
        h.configure(padding=12)
        tk.Label(h, text="Capteurs 433 MHz (rtl_433)", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION).pack(side="left")
        self.s433_pill = theme.pill(h, "INCONNU", theme.MUTED)
        self.s433_pill.pack(side="left", padx=(12, 0))
        self.s433_info = tk.Label(h, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.s433_info.pack(side="left", padx=(14, 0))
        ttk.Button(h, text="Effacer", style="Flat.TButton", command=self._s433_clear).pack(side="right")
        ttk.Button(h, text="Écouter le 433 MHz", style="Accent.TButton",
                   command=lambda: self._switch_sdr("433")).pack(side="right", padx=(0, 8))

        body = tk.Frame(outer, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        tbl = theme.card(body)
        tbl.pack(side="left", fill="both", expand=True)
        cols = ("name", "model", "id", "last", "count", "batt", "rssi", "summary")
        self.s433_tree = ttk.Treeview(tbl.inner, columns=cols, show="headings")
        spec = {"name": ("Nom", 140, "w"), "model": ("Modèle", 170, "w"), "id": ("ID / canal", 90, "center"),
                "last": ("Dernière réc.", 100, "center"), "count": ("Nb", 50, "center"),
                "batt": ("Pile", 60, "center"), "rssi": ("Signal", 80, "center"), "summary": ("Mesures", 420, "w")}
        for c in cols:
            t, w, a = spec[c]
            self.s433_tree.heading(c, text=t)
            self.s433_tree.column(c, width=w, minwidth=40, anchor=a)
        self.s433_tree.tag_configure("old", foreground=theme.MUTED)
        vsb = ttk.Scrollbar(tbl.inner, orient="vertical", command=self.s433_tree.yview)
        self.s433_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.s433_tree.pack(fill="both", expand=True, padx=1, pady=1)
        self.s433_tree.bind("<<TreeviewSelect>>", self._s433_select)
        self.s433_tree.bind("<Double-1>", self._s433_rename)

        det = theme.card(body)
        det.pack(side="left", fill="y", padx=(10, 0))
        d = det.inner
        d.configure(padding=12)
        d.configure(width=380)
        self.s433_title = tk.Label(d, text="Sélectionnez un capteur.", bg=theme.BG_CARD, fg=theme.TEXT,
                                   font=("Segoe UI", 13, "bold"), anchor="w", justify="left", wraplength=360)
        self.s433_title.pack(anchor="w")
        tk.Label(d, text="Double-clic sur une ligne pour donner un nom au capteur.", bg=theme.BG_CARD, fg=theme.MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(2, 8))
        self.s433_vals = tk.Frame(d, bg=theme.BG_CARD)
        self.s433_vals.pack(anchor="w", fill="x")
        self._s433_devices = []
        self._s433_sel = None
        self._s433_poll_running = False

    def _s433_name(self, dev):
        return (self.config_data.get("sensor_names") or {}).get(dev.get("key"), "")

    def _s433_summary(self, vals):
        parts = []
        for k, v in vals.items():
            label, fmt = self.S433_FIELDS.get(k, (None, None))
            if not label:
                continue
            try:
                parts.append(f"{label} {fmt.format(v)}")
            except (ValueError, TypeError):
                parts.append(f"{label} {v}")
        return "  ·  ".join(parts[:5])

    def _fetch_s433(self):
        host = self.config_data.get("pi_host", "192.168.1.229")
        port = self.config_data.get("port_433", 5010)
        try:
            data = {"status": api_client.get_433(host, port, "status"), "devices": api_client.get_433(host, port, "devices")}
        except Exception as exc:
            self.after(0, self._render_s433, None, str(exc))
            return
        self.after(0, self._render_s433, data, None)

    def _s433_poll(self):
        if getattr(self, "_current_view", None) != "s433":
            self._s433_poll_running = False
            return
        threading.Thread(target=self._fetch_s433, daemon=True).start()
        self.after(5000, self._s433_poll)

    def _render_s433(self, data, error):
        if not hasattr(self, "s433_tree"):
            return
        mode = getattr(self, "_sdr_mode", None)
        if error:
            if mode not in (None, "433"):
                self.s433_pill.config(text="  EN PAUSE  ", bg=theme.MUTED)
                self.s433_info.config(text=f"La clé SDR est sur {SDR_MODE_LABELS.get(mode, mode)}.")
            else:
                self.s433_pill.config(text="  NON INSTALLÉ / INJOIGNABLE  ", bg=theme.MUTED)
                self.s433_info.config(text="Service rtl433 (port 5010) injoignable : lancer install_extras.sh sur le Pi.")
            return
        st = data["status"]
        self.s433_pill.config(text="  ÉCOUTE  " if st.get("running") else "  ARRÊTÉ  ",
                              bg=theme.GREEN if st.get("running") else theme.RED)
        self.s433_info.config(text=(f"{st.get('freq')}  ·  {st.get('devices', 0)} capteur(s), dont "
                                    f"{st.get('active_last_hour', 0)} actif(s) la dernière heure  ·  "
                                    f"{st.get('messages', 0)} message(s) depuis le démarrage"
                                    + (f"  ·  ⚠ {st['error']}" if st.get("error") else "")))
        self._s433_devices = data["devices"] or []
        tree = self.s433_tree
        tree.delete(*tree.get_children())
        now = time.time()
        for dev in self._s433_devices:
            ls = dev.get("last_seen") or 0
            batt = {1: "OK", 0: "FAIBLE"}.get(dev.get("battery_ok"), "")
            rssi = f"{dev['rssi']:.0f} dB" if isinstance(dev.get("rssi"), (int, float)) else ""
            idc = f"{dev.get('id', '')}" + (f" / {dev['channel']}" if dev.get("channel") not in (None, "") else "")
            tree.insert("", "end", iid=dev["key"], tags=("old",) if now - ls > 3600 else (), values=(
                self._s433_name(dev), dev.get("model") or "", idc,
                time.strftime("%H:%M:%S", time.localtime(ls)) if ls else "", dev.get("count", 0), batt, rssi,
                self._s433_summary(dev.get("values") or {})))
        if self._s433_sel and tree.exists(self._s433_sel):
            tree.selection_set(self._s433_sel)
        if "s433" in self.tile_chips:
            act = sum(1 for d in self._s433_devices if now - (d.get("last_seen") or 0) < 3600)
            self.tile_chips["s433"].config(text=f"{len(self._s433_devices)} capteur(s) · {act} actif(s)")

    def _s433_select(self, _e=None):
        sel = self.s433_tree.selection()
        if not sel:
            return
        self._s433_sel = sel[0]
        dev = next((d for d in self._s433_devices if d["key"] == sel[0]), None)
        if not dev:
            return
        name = self._s433_name(dev)
        self.s433_title.config(text=(f"{name}\n" if name else "") + f"{dev.get('model')} (ID {dev.get('id')})")
        for w in self.s433_vals.winfo_children():
            w.destroy()
        rows = []
        for k, v in (dev.get("values") or {}).items():
            label, fmt = self.S433_FIELDS.get(k, (k, "{}"))
            try:
                rows.append((label, fmt.format(v)))
            except (ValueError, TypeError):
                rows.append((label, str(v)))
        rows += [("Reçu", f"{dev.get('count', 0)} fois"),
                 ("Première réception", time.strftime("%d/%m %H:%M", time.localtime(dev.get("first_seen", 0)))),
                 ("Dernière réception", time.strftime("%d/%m %H:%M:%S", time.localtime(dev.get("last_seen", 0))))]
        if isinstance(dev.get("snr"), (int, float)):
            rows.append(("RSSI / SNR", f"{dev.get('rssi', 0):.1f} / {dev['snr']:.1f} dB"))
        for i, (a, b) in enumerate(rows):
            tk.Label(self.s433_vals, text=a, bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE).grid(
                row=i, column=0, sticky="w", pady=1)
            tk.Label(self.s433_vals, text=b, bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY_BOLD).grid(
                row=i, column=1, sticky="e", padx=(16, 0), pady=1)

    def _s433_rename(self, _e=None):
        sel = self.s433_tree.selection()
        if not sel:
            return
        dev = next((d for d in self._s433_devices if d["key"] == sel[0]), None)
        if not dev:
            return
        name = simpledialog.askstring("Nom du capteur", f"Nom pour « {dev.get('model')} (ID {dev.get('id')}) » :",
                                      initialvalue=self._s433_name(dev), parent=self)
        if name is None:
            return
        names = dict(self.config_data.get("sensor_names") or {})
        if name.strip():
            names[dev["key"]] = name.strip()
        else:
            names.pop(dev["key"], None)
        self.config_data["sensor_names"] = names
        try:
            save_config(self.config_data)
        except Exception:
            pass
        threading.Thread(target=self._fetch_s433, daemon=True).start()

    def _s433_clear(self):
        if not messagebox.askyesno("Capteurs 433", "Effacer la liste des capteurs reçus ?"):
            return
        host = self.config_data.get("pi_host", "192.168.1.229")

        def w():
            try:
                requests.post(f"http://{host}:{self.config_data.get('port_433', 5010)}/api/433/clear", timeout=6)
            except Exception:
                pass
            self._fetch_s433()

        threading.Thread(target=w, daemon=True).start()

    # --------------------------------------------------------- Satellites --

    def _build_sat_tab(self, frame):
        outer = tk.Frame(frame, bg=theme.BG)
        outer.pack(fill="both", expand=True, padx=4, pady=4)
        left = tk.Frame(outer, bg=theme.BG, width=820)
        left.pack(side="left", fill="y", pady=(0, 6))
        left.pack_propagate(False)

        top = theme.card(left)
        top.pack(fill="x", padx=12, pady=(8, 6))
        t = top.inner
        t.configure(padding=12)
        tk.Label(t, text="PROCHAIN PASSAGE DE L'ISS", bg=theme.BG_CARD, fg=theme.MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.sat_iss_big = tk.Label(t, text="—", bg=theme.BG_CARD, fg=theme.ACCENT, font=("Segoe UI", 18, "bold"))
        self.sat_iss_big.pack(anchor="w")
        self.sat_iss_sub = tk.Label(t, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE,
                                    justify="left", anchor="w")
        self.sat_iss_sub.pack(anchor="w")
        opt = tk.Frame(t, bg=theme.BG_CARD)
        opt.pack(anchor="w", pady=(8, 0))
        self.sat_auto_var = tk.BooleanVar(value=self.config_data.get("iss_auto", False))
        tk.Checkbutton(opt, text="Pendant les passages de l'ISS, donner automatiquement la clé SDR à « APRS ISS » "
                                 "(145,825 MHz) puis la rendre au module précédent", variable=self.sat_auto_var,
                       command=self._sat_toggle_auto, bg=theme.BG_CARD, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT,
                       activebackground=theme.BG_CARD, activeforeground=theme.TEXT, font=theme.FONT_SUBTITLE,
                       highlightthickness=0, wraplength=760, justify="left").pack(anchor="w")
        ttk.Button(opt, text="Écouter l'APRS de l'ISS maintenant", style="Flat.TButton",
                   command=lambda: self._switch_sdr("iss")).pack(anchor="w", pady=(4, 0))

        bar = tk.Frame(left, bg=theme.BG)
        bar.pack(fill="x", padx=12, pady=(2, 4))
        tk.Label(bar, text="Passages (24 h)", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION).pack(side="left")
        self.sat_all_var = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="tous les satellites amateurs", variable=self.sat_all_var,
                       command=self._sat_recompute, bg=theme.BG, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT,
                       activebackground=theme.BG, activeforeground=theme.TEXT, font=theme.FONT_SUBTITLE,
                       highlightthickness=0).pack(side="left", padx=(12, 0))
        tk.Label(bar, text="élévation min.", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE).pack(side="left", padx=(12, 4))
        self.sat_minel_var = tk.StringVar(value="10")
        sb = ttk.Spinbox(bar, from_=0, to=60, width=4, textvariable=self.sat_minel_var, command=self._sat_recompute)
        sb.pack(side="left")
        sb.bind("<Return>", lambda _e: self._sat_recompute())
        self.sat_tle_label = tk.Label(bar, text="", bg=theme.BG, fg=theme.MUTED, font=("Segoe UI", 8))
        self.sat_tle_label.pack(side="right")

        tbl = theme.card(left)
        tbl.pack(fill="x", padx=12, pady=(0, 6))
        cols = ("sat", "aos", "maxel", "dur", "az", "info")
        self.sat_tree = ttk.Treeview(tbl.inner, columns=cols, show="headings", height=9)
        spec = {"sat": ("Satellite", 150, "w"), "aos": ("Lever", 110, "center"), "maxel": ("Élév. max", 76, "center"),
                "dur": ("Durée", 60, "center"), "az": ("Azimut lever → coucher", 150, "center"),
                "info": ("Dans", 90, "center")}
        for c in cols:
            tt, w, a = spec[c]
            self.sat_tree.heading(c, text=tt)
            self.sat_tree.column(c, width=w, minwidth=40, anchor=a)
        self.sat_tree.tag_configure("now", background="#14532d", foreground="#ffffff")
        self.sat_tree.tag_configure("good", foreground="#4ade80")
        vsb = ttk.Scrollbar(tbl.inner, orient="vertical", command=self.sat_tree.yview)
        self.sat_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.sat_tree.pack(fill="x", padx=1, pady=1)
        self.sat_tree.bind("<<TreeviewSelect>>", self._sat_select)

        det = theme.card(left)
        det.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        d = det.inner
        d.configure(padding=8)
        self.sat_polar = tk.Canvas(d, width=250, height=250, bg=theme.BG_CARD, highlightthickness=0)
        self.sat_polar.pack(side="left")
        info = tk.Frame(d, bg=theme.BG_CARD)
        info.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.sat_det_title = tk.Label(info, text="Sélectionnez un passage.", bg=theme.BG_CARD, fg=theme.TEXT,
                                      font=("Segoe UI", 13, "bold"), anchor="w")
        self.sat_det_title.pack(anchor="w")
        self.sat_det_freq = tk.Label(info, text="", bg=theme.BG_CARD, fg=theme.ACCENT, font=theme.FONT_SUBTITLE,
                                     anchor="w", justify="left", wraplength=500)
        self.sat_det_freq.pack(anchor="w", pady=(2, 6))
        self.sat_det_text = tk.Label(info, text="", bg=theme.BG_CARD, fg=theme.TEXT, font=("Consolas", 9),
                                     anchor="nw", justify="left")
        self.sat_det_text.pack(anchor="w", fill="both", expand=True)

        right = tk.Frame(outer, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True, pady=(8, 12), padx=(0, 12))
        mo = theme.card(right)
        mo.pack(fill="both", expand=True)
        self.sat_map = self._make_map(mo.inner)
        self.sat_map.pack(fill="both", expand=True, padx=1, pady=1)
        self.sat_map.set_position(*DEFAULT_COORDS)
        self.sat_map.set_zoom(3)
        tk.Label(right, text="Position actuelle des satellites suivis · trace des 90 prochaines minutes et zone de "
                             "visibilité du satellite sélectionné", bg=theme.BG, fg=theme.MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))

        self._sats = []
        self._sat_passes = []
        self._sat_selected = None
        self._sat_markers = []
        self._sat_obs = sat_predict.Observer(*DEFAULT_COORDS, 100)
        self._iss_auto_prev = None
        threading.Thread(target=self._sat_load, daemon=True).start()
        self.after(15000, self._sat_tick)

    def _sat_load(self, force=False):
        try:
            tles, when = sat_predict.load_tles(force)
            sats = [sat_predict.Sat(*x) for x in tles]
            err = None
        except Exception as exc:
            sats, when, err = [], None, str(exc)
        self.after(0, self._sat_loaded, sats, when, err)

    def _sat_loaded(self, sats, when, err):
        if err:
            self.sat_tle_label.config(text=f"⚠ {err}", fg=theme.ORANGE)
            return
        self._sats = sats
        self.sat_tle_label.config(text=f"{len(sats)} satellites · éléments CelesTrak du "
                                       f"{time.strftime('%d/%m %H:%M', time.localtime(when))}", fg=theme.MUTED)
        self._sat_recompute()

    def _sat_recompute(self):
        try:
            min_el = float(self.sat_minel_var.get())
        except ValueError:
            min_el = 10
        sats = self._sats if self.sat_all_var.get() else [s for s in self._sats if s.label]
        obs = self._sat_obs

        def worker():
            now = time.time()
            res = []
            for s in sats:
                try:
                    for p in s.passes(obs, now - 900, hours=24, min_el=min_el):
                        if p["los"] > now:
                            p["sat"] = s
                            res.append(p)
                except Exception:
                    continue
            res.sort(key=lambda p: p["aos"])
            self.after(0, self._sat_show, res)

        threading.Thread(target=worker, daemon=True).start()

    def _sat_show(self, passes):
        self._sat_passes = passes
        self._sat_refresh_table()
        self._sat_tick(reschedule=False)

    def _sat_refresh_table(self):
        tree = self.sat_tree
        sel = self._sat_selected
        tree.delete(*tree.get_children())
        now = time.time()
        for i, p in enumerate(self._sat_passes[:80]):
            s = p["sat"]
            on = p["aos"] <= now <= p["los"]
            dt = p["aos"] - now
            when = "EN COURS" if on else (f"{int(dt // 3600)} h {int(dt % 3600 // 60):02d}" if dt >= 3600 else f"{int(dt // 60)} min")
            tree.insert("", "end", iid=str(i), tags=("now",) if on else (("good",) if p["max_el"] >= 40 else ()), values=(
                s.label or s.name, time.strftime("%d/%m %H:%M:%S", time.localtime(p["aos"])), f"{p['max_el']:.0f}°",
                f"{int((p['los'] - p['aos']) // 60)} min", f"{p['aos_az']:.0f}° {self._compass(p['aos_az'])} → "
                f"{p['los_az']:.0f}° {self._compass(p['los_az'])}", when))
        if sel is not None and tree.exists(sel):
            tree.selection_set(sel)

    def _sat_select(self, _e=None):
        sel = self.sat_tree.selection()
        if not sel:
            return
        self._sat_selected = sel[0]
        idx = int(sel[0])
        if idx >= len(self._sat_passes):
            return
        p = self._sat_passes[idx]
        s = p["sat"]
        self.sat_det_title.config(text=s.label or s.name)
        self.sat_det_freq.config(text=s.freqs or "")
        obs = self._sat_obs
        lines = [f"{'Heure':8} {'Azim.':>6} {'Élév.':>6} {'Dist.':>7}  Doppler 145,825 / 437,800"]
        t = p["aos"]
        while t <= p["los"] + 1:
            az, el, rng, rr = s.look(obs, t)
            d2 = -rr / 299792.458 * 145.825e6
            d7 = -rr / 299792.458 * 437.8e6
            lines.append(f"{time.strftime('%H:%M:%S', time.localtime(t))} {az:5.0f}° {el:5.0f}° {rng:6.0f} km  "
                         f"{d2 / 1000:+5.1f} / {d7 / 1000:+5.1f} kHz")
            t += 60
        self.sat_det_text.config(text="\n".join(lines[:16]))
        self._sat_draw_polar(p)
        self._sat_draw_map()

    def _sat_draw_polar(self, p):
        c = self.sat_polar
        c.delete("all")
        cx, cy, r = 125, 125, 105
        for el in (0, 30, 60):
            rr = r * (90 - el) / 90
            c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=theme.BORDER)
            c.create_text(cx + 3, cy - rr + 7, text=f"{el}°", anchor="w", fill=theme.MUTED, font=("Segoe UI", 7))
        for a, lab in ((0, "N"), (90, "E"), (180, "S"), (270, "O")):
            ra = math.radians(a)
            c.create_line(cx, cy, cx + r * math.sin(ra), cy - r * math.cos(ra), fill=theme.BORDER)
            c.create_text(cx + (r + 10) * math.sin(ra), cy - (r + 10) * math.cos(ra), text=lab, fill=theme.MUTED,
                          font=("Segoe UI", 8, "bold"))
        s, obs = p["sat"], self._sat_obs
        pts = []
        t = p["aos"]
        while t <= p["los"]:
            az, el, _r, _v = s.look(obs, t)
            rr = r * (90 - max(0, el)) / 90
            pts += [cx + rr * math.sin(math.radians(az)), cy - rr * math.cos(math.radians(az))]
            t += 15
        if len(pts) >= 4:
            c.create_line(*pts, fill=theme.ACCENT, width=2, smooth=True)
            c.create_oval(pts[0] - 4, pts[1] - 4, pts[0] + 4, pts[1] + 4, fill="#4ade80", outline="")
            c.create_oval(pts[-2] - 4, pts[-1] - 4, pts[-2] + 4, pts[-1] + 4, fill="#f87171", outline="")
        now = time.time()
        if p["aos"] <= now <= p["los"]:
            az, el, _r, _v = s.look(obs, now)
            rr = r * (90 - max(0, el)) / 90
            x, y = cx + rr * math.sin(math.radians(az)), cy - rr * math.cos(math.radians(az))
            c.create_oval(x - 6, y - 6, x + 6, y + 6, outline="#ffffff", width=2)

    def _sat_draw_map(self):
        for m in self._sat_markers:
            try:
                m.delete()
            except Exception:
                pass
        self._sat_markers = []
        self.sat_map.delete_all_path()
        self.sat_map.delete_all_polygon()
        now = time.time()
        shown = [s for s in self._sats if s.label]
        sel = None
        if self._sat_selected is not None and int(self._sat_selected) < len(self._sat_passes):
            sel = self._sat_passes[int(self._sat_selected)]["sat"]
        for s in shown:
            sp = s.subpoint(now)
            if not sp:
                continue
            is_sel = s is sel
            self._sat_markers.append(self.sat_map.set_marker(
                sp[0], sp[1], text=s.label.split(" ")[0], text_color="#7e22ce" if not is_sel else "#c2410c",
                marker_color_circle="#581c87" if not is_sel else "#7c2d12",
                marker_color_outside="#c084fc" if not is_sel else "#fb923c"))
        if sel is not None:
            seg, prev_lon = [], None
            for k in range(0, 91, 1):
                sp = sel.subpoint(now + k * 60)
                if not sp:
                    continue
                if prev_lon is not None and abs(sp[1] - prev_lon) > 180:  # passage de l'antiméridien
                    if len(seg) >= 2:
                        self.sat_map.set_path(seg, color="#fb923c", width=2)
                    seg = []
                seg.append((sp[0], sp[1]))
                prev_lon = sp[1]
            if len(seg) >= 2:
                self.sat_map.set_path(seg, color="#fb923c", width=2)
            fp = sat_predict.footprint(sel, now)
            if fp and max(abs(a[1] - b[1]) for a, b in zip(fp, fp[1:])) < 180:
                self.sat_map.set_polygon(fp, outline_color="#fb923c", fill_color=None, border_width=1)
        self._sat_markers.append(self.sat_map.set_marker(*DEFAULT_COORDS, text="F4JCF", text_color="#16a34a",
                                                         marker_color_circle="#14532d", marker_color_outside="#22c55e"))

    def _sat_toggle_auto(self):
        self.config_data["iss_auto"] = bool(self.sat_auto_var.get())
        try:
            save_config(self.config_data)
        except Exception:
            pass

    def _sat_tick(self, reschedule=True):
        """Toutes les 15 s : compte à rebours ISS, carte, bascule auto ISS."""
        try:
            now = time.time()
            iss = [p for p in self._sat_passes if (p["sat"].label or "").startswith("ISS")]
            nxt = next((p for p in iss if p["los"] > now), None)
            if nxt:
                if nxt["aos"] <= now:
                    az, el, _r, _v = nxt["sat"].look(self._sat_obs, now)
                    self.sat_iss_big.config(text=f"L'ISS est au-dessus de vous ! Élévation {el:.0f}°, azimut {az:.0f}°",
                                            fg="#4ade80")
                    self.sat_iss_sub.config(text=f"Coucher à {time.strftime('%H:%M:%S', time.localtime(nxt['los']))} "
                                                 f"vers {nxt['los_az']:.0f}° {self._compass(nxt['los_az'])}")
                else:
                    dt = nxt["aos"] - now
                    self.sat_iss_big.config(
                        text=(f"Dans {int(dt // 3600)} h {int(dt % 3600 // 60):02d} min" if dt >= 3600 else f"Dans {int(dt // 60)} min {int(dt % 60):02d} s"),
                        fg=theme.ACCENT)
                    self.sat_iss_sub.config(text=(
                        f"Lever à {time.strftime('%H:%M:%S', time.localtime(nxt['aos']))} au {nxt['aos_az']:.0f}° "
                        f"{self._compass(nxt['aos_az'])}  ·  élévation max {nxt['max_el']:.0f}° à "
                        f"{time.strftime('%H:%M', time.localtime(nxt['max_t']))}  ·  coucher à "
                        f"{time.strftime('%H:%M:%S', time.localtime(nxt['los']))}"))
                if "sats" in self.tile_chips:
                    self.tile_chips["sats"].config(
                        text="ISS au-dessus !" if nxt["aos"] <= now else
                        f"ISS à {time.strftime('%H:%M', time.localtime(nxt['aos']))} ({nxt['max_el']:.0f}°)")
            # Bascule automatique vers l'APRS de l'ISS pendant le passage
            if self.config_data.get("iss_auto") and nxt is not None:
                active = nxt["aos"] - 30 <= now <= nxt["los"]
                mode = getattr(self, "_sdr_mode", None)
                if active and mode != "iss" and self._iss_auto_prev is None and mode != "off":
                    self._iss_auto_prev = mode or "aprs"
                    self._switch_sdr("iss")
                elif not active and self._iss_auto_prev is not None:
                    prev, self._iss_auto_prev = self._iss_auto_prev, None
                    if mode == "iss":
                        self._switch_sdr(prev)
            if getattr(self, "_current_view", None) == "sats":
                self._sat_refresh_table()
                self._sat_draw_map()
            if self._sat_passes and self._sat_passes[0]["los"] < now - 60:
                self._sat_recompute()
        except Exception as exc:
            _log_crash(f"Satellites : {exc}", context="sat_tick")
        if reschedule:
            if self._sats and time.time() - getattr(self, "_sat_tle_checked", 0) > 6 * 3600:
                self._sat_tle_checked = time.time()
                threading.Thread(target=self._sat_load, daemon=True).start()
            self.after(15000, self._sat_tick)

    # ------------------------------------------------- Pages informatives --

    def _build_info_tab(self, frame, title: str, status_key: str, body_text: str):
        """Page simple pour un module dont le Pi n'expose pas (encore)
        d'API dédiée : titre, statut (mis à jour depuis les services
        système quand un service correspondant est trouvé) et explication
        honnête de ce qui manque pour aller plus loin. Réutilisé par
        Winlink, AIS et SvxLink."""
        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        outer = theme.card(wrapper)
        outer.pack(fill="x", padx=12, pady=12)
        inner = outer.inner
        inner.configure(padding=18)

        tk.Label(
            inner, text=title, bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION,
        ).pack(anchor="w")
        status_pill = theme.pill(inner, "NON CONFIGURÉ", theme.MUTED)
        status_pill.pack(anchor="w", pady=(10, 14))
        if not hasattr(self, "_info_status_pills"):
            self._info_status_pills = {}
        self._info_status_pills[status_key] = status_pill

        tk.Label(
            inner, text=body_text, bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY, justify="left",
        ).pack(anchor="w")

        if status_key in self.tile_chips:
            self.tile_chips[status_key].config(text="Non configuré")

    # ---------------------------------------------------------- Services --

    def _render_services(self, data, error):
        frame = self.pages["services"]
        if error:
            self._render_error(frame, error)
            return
        self._last_services = data
        self._clear(frame)

        total = len(data)
        running = sum(1 for v in data.values() if v.get("running"))
        self.chip_services.config(text=f"Services : {running}/{total}")
        if "services" in self.tile_chips:
            self.tile_chips["services"].config(text=f"{running}/{total} actifs")

        direwolf = data.get("direwolf")
        if direwolf is not None:
            is_running = bool(direwolf.get("running"))
            text = "EN COURS" if is_running else "ARRÊTÉ"
            color = theme.GREEN if is_running else theme.RED
            self.aprs_badge.config(text=f"  {text}  ", bg=color)
            if "aprs" in self.tile_chips:
                self.tile_chips["aprs"].config(text=text)

        # AIS et SvxLink : on ne connaît pas la clé exacte utilisée côté Pi
        # pour ces services, donc on cherche par correspondance de nom
        # plutôt que de supposer une clé figée (voir _find_service).
        for status_key, fragments in (("ais", ("ais",)), ("svxlink", ("svxlink", "svx"))):
            info = _find_service(data, *fragments)
            if info is None:
                continue  # service absent de la réponse : on garde "Non configuré"
            is_running = bool(info.get("running"))
            text = "EN COURS" if is_running else "ARRÊTÉ"
            color = theme.GREEN if is_running else theme.RED
            if status_key in self.tile_chips:
                self.tile_chips[status_key].config(text=text)
            pill = getattr(self, "_info_status_pills", {}).get(status_key)
            if pill is not None:
                pill.config(text=f"  {text}  ", bg=color)

        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        outer = theme.card(wrapper)
        outer.pack(fill="both", expand=True, padx=12, pady=12)
        inner = outer.inner
        inner.configure(padding=12)

        for i, (key, info) in enumerate(sorted(data.items())):
            label = info.get("label", key)
            is_running = bool(info.get("running"))

            row = tk.Frame(inner, bg=theme.BG_CARD_ALT if i % 2 else theme.BG_CARD)
            row.pack(fill="x", pady=1)

            dot = theme.status_dot(row, theme.GREEN if is_running else theme.RED)
            dot.configure(bg=row["bg"])
            dot.pack(side="left", padx=(10, 8), pady=8)

            tk.Label(
                row, text=label, bg=row["bg"], fg=theme.TEXT, font=theme.FONT_BODY,
            ).pack(side="left", padx=(0, 12), pady=8)

            badge = theme.pill(row, "EN COURS" if is_running else "ARRÊTÉ", theme.GREEN if is_running else theme.RED)
            badge.pack(side="right", padx=10, pady=6)

            restart_btn = ttk.Button(
                row, text="Redémarrer", style="Flat.TButton",
                command=lambda k=key: self._restart_service(k),
            )
            restart_btn.pack(side="right", padx=(0, 10), pady=6)

    def _restart_service(self, key):
        host = self.config_data.get("pi_host", "192.168.1.229")
        port = self.config_data["port_services"]

        def _do():
            try:
                api_client.restart_service(host, port, key)
            except requests.RequestException as exc:
                self.after(0, messagebox.showerror, "Redémarrage", f"Échec du redémarrage de {key} :\n{exc}")
                return
            self.after(500, self._refresh_once)

        threading.Thread(target=_do, daemon=True).start()

    # ---------------------------------------------------------------- SDR --

    def _render_sdr_mode(self, data, error):
        """Mémorise le module qui détient la clé (pour ne pas rebasculer
        inutilement) et met à jour la page de pilotage et les vignettes."""
        if error:
            self._sdr_switch_data = None
            if hasattr(self, "sdr_mode_value"):
                self.sdr_mode_value.config(text="Service de bascule injoignable", fg=theme.RED)
            return
        self._sdr_switch_data = data
        if data.get("last_mode"):
            self._sdr_last_mode = data["last_mode"]
        self._render_pi_version(data)
        if getattr(self, "_sdr_switching", False):
            return
        mode = data.get("mode")
        if mode:
            self._sdr_mode = mode
            self.chip_sdr.config(text=f"Clé SDR : {SDR_MODE_LABELS.get(mode, mode)}")
        self._refresh_all_tiles()
        self._render_sdr_control(data)

    def _render_sdr(self, data, error):
        """Ancien gestionnaire de tuners (port 5002) : remplacé par la page
        de pilotage de la clé unique, conservé pour compatibilité."""
        return

    def _build_sdr_tab(self, frame):
        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        # --- Module actif + boutons
        top_outer = theme.card(wrapper)
        top_outer.pack(fill="x", padx=12, pady=(8, 8))
        top = top_outer.inner
        top.configure(padding=16)
        tk.Label(top, text="La clé SDR est sur :", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE).pack(anchor="w")
        self.sdr_mode_value = tk.Label(top, text="—", bg=theme.BG_CARD, fg=theme.ACCENT, font=("Segoe UI", 22, "bold"))
        self.sdr_mode_value.pack(anchor="w", pady=(2, 2))
        self.sdr_mode_detail = tk.Label(top, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY)
        self.sdr_mode_detail.pack(anchor="w", pady=(0, 10))
        btns = tk.Frame(top, bg=theme.BG_CARD)
        btns.pack(anchor="w")
        tk.Label(btns, text="Donner la clé à :", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY).pack(side="left", padx=(0, 10))
        self.sdr_mode_buttons = {}
        for mode in ("adsb", "ais", "aprs", "iss", "beacon", "elt121", "433", "openwebrx"):
            b = tk.Button(
                btns, text=SDR_MODE_LABELS[mode], font=theme.FONT_BODY_BOLD, relief="flat", bd=0,
                bg=theme.BG_CARD_ALT, fg=theme.TEXT, activebackground=theme.ACCENT, activeforeground="#1a1200",
                padx=16, pady=6, cursor="hand2", command=lambda m=mode: self._switch_sdr(m),
            )
            b.pack(side="left", padx=4)
            self.sdr_mode_buttons[mode] = b
        self.sdr_stop_button = tk.Button(
            btns, text="■  Arrêter la clé SDR", font=theme.FONT_BODY_BOLD, relief="flat", bd=0,
            bg=theme.RED, fg="#0b0f17", activebackground="#ff7b72", activeforeground="#0b0f17",
            padx=16, pady=6, cursor="hand2", command=self._stop_sdr,
        )
        self.sdr_stop_button.pack(side="left", padx=(24, 4))

        # --- Planning
        plan_outer = theme.card(wrapper)
        plan_outer.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        plan = plan_outer.inner
        plan.configure(padding=16)
        tk.Label(plan, text="Partage automatique de la clé", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION).pack(anchor="w")
        tk.Label(
            plan, text=(
                "Manuel : la clé reste sur le module choisi.  Rotation : les modules se succèdent en boucle.  "
                "Planning : un module par créneau horaire.\nOuvrir une page dans l'appli prend la clé et met le "
                "partage automatique en pause pendant la durée indiquée ci-dessous."
            ),
            bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE, justify="left",
        ).pack(anchor="w", pady=(4, 10))

        self.sdr_sched_type = tk.StringVar(value="manual")
        type_row = tk.Frame(plan, bg=theme.BG_CARD)
        type_row.pack(anchor="w")
        for val, txt in (("manual", "Manuel"), ("rotation", "Rotation"), ("planning", "Planning horaire")):
            tk.Radiobutton(
                type_row, text=txt, value=val, variable=self.sdr_sched_type, bg=theme.BG_CARD, fg=theme.TEXT,
                selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG_CARD, activeforeground=theme.TEXT,
                font=theme.FONT_BODY, highlightthickness=0, command=self._sdr_sched_show,
            ).pack(side="left", padx=(0, 16))

        mode_names = [SDR_MODE_LABELS[m] for m in ("adsb", "ais", "aprs", "iss", "beacon", "elt121", "433", "openwebrx")]
        self._sdr_label_to_mode = {v: k for k, v in SDR_MODE_LABELS.items()}

        editors = tk.Frame(plan, bg=theme.BG_CARD)
        editors.pack(anchor="w", fill="x", pady=(10, 0))

        self.sdr_rot_frame = tk.Frame(editors, bg=theme.BG_CARD)
        tk.Label(self.sdr_rot_frame, text="Ordre de passage (module · durée en minutes) :", bg=theme.BG_CARD,
                 fg=theme.TEXT, font=theme.FONT_BODY).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
        self.sdr_rot_rows = []
        for i in range(6):
            mv, nv = tk.StringVar(value=""), tk.StringVar(value="")
            tk.Label(self.sdr_rot_frame, text=f"{i + 1}.", bg=theme.BG_CARD, fg=theme.MUTED,
                     font=theme.FONT_BODY).grid(row=i + 1, column=0, sticky="e", padx=(0, 6))
            ttk.Combobox(self.sdr_rot_frame, textvariable=mv, values=[""] + mode_names, width=12,
                         state="readonly").grid(row=i + 1, column=1, pady=2)
            ttk.Spinbox(self.sdr_rot_frame, textvariable=nv, from_=1, to=1440, width=6).grid(row=i + 1, column=2, padx=6)
            tk.Label(self.sdr_rot_frame, text="min", bg=theme.BG_CARD, fg=theme.MUTED,
                     font=theme.FONT_SUBTITLE).grid(row=i + 1, column=3, sticky="w")
            self.sdr_rot_rows.append((mv, nv))

        self.sdr_plan_frame = tk.Frame(editors, bg=theme.BG_CARD)
        tk.Label(self.sdr_plan_frame, text="À partir de (HH:MM) · module :", bg=theme.BG_CARD,
                 fg=theme.TEXT, font=theme.FONT_BODY).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.sdr_plan_rows = []
        for i in range(6):
            sv, mv = tk.StringVar(value=""), tk.StringVar(value="")
            tk.Label(self.sdr_plan_frame, text=f"{i + 1}.", bg=theme.BG_CARD, fg=theme.MUTED,
                     font=theme.FONT_BODY).grid(row=i + 1, column=0, sticky="e", padx=(0, 6))
            ttk.Entry(self.sdr_plan_frame, textvariable=sv, width=7).grid(row=i + 1, column=1, pady=2)
            ttk.Combobox(self.sdr_plan_frame, textvariable=mv, values=[""] + mode_names, width=12,
                         state="readonly").grid(row=i + 1, column=2, padx=6)
            self.sdr_plan_rows.append((sv, mv))

        bottom = tk.Frame(plan, bg=theme.BG_CARD)
        bottom.pack(anchor="w", fill="x", pady=(12, 0))
        tk.Label(bottom, text="Pause après une bascule manuelle :", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_BODY).pack(side="left")
        self.sdr_hold_var = tk.StringVar(value="15")
        ttk.Spinbox(bottom, textvariable=self.sdr_hold_var, from_=0, to=240, width=5).pack(side="left", padx=6)
        tk.Label(bottom, text="min", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE).pack(side="left")
        ttk.Button(bottom, text="Enregistrer", style="Accent.TButton", command=self._sdr_save_schedule).pack(side="left", padx=(20, 0))
        self.sdr_sched_status = tk.Label(bottom, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.sdr_sched_status.pack(side="left", padx=(12, 0))

        self._sdr_sched_loaded = False
        self._sdr_sched_show()

    def _stop_sdr(self):
        if getattr(self, "_sdr_mode", None) == "off":
            return
        if not messagebox.askyesno(
            "Arrêter la clé SDR",
            "Arrêter tous les modules qui utilisent la clé SDR (ADS-B, AIS, APRS, OpenWebRX) ?\n\n"
            "Le partage automatique est suspendu. Pour relancer, donnez la clé à un module "
            "(bouton ou ouverture de sa page).",
        ):
            return
        self._switch_sdr("off")

    def _sdr_sched_show(self):
        self.sdr_rot_frame.pack_forget()
        self.sdr_plan_frame.pack_forget()
        t = self.sdr_sched_type.get()
        if t == "rotation":
            self.sdr_rot_frame.pack(anchor="w")
        elif t == "planning":
            self.sdr_plan_frame.pack(anchor="w")

    def _sdr_fill_schedule(self, sched):
        self.sdr_sched_type.set(sched.get("type", "manual"))
        rot = sched.get("rotation") or []
        for i, (mv, nv) in enumerate(self.sdr_rot_rows):
            if i < len(rot):
                mv.set(SDR_MODE_LABELS.get(rot[i][0], rot[i][0]))
                nv.set(str(rot[i][1]))
            else:
                mv.set("")
                nv.set("")
        plan = sched.get("planning") or []
        for i, (sv, mv) in enumerate(self.sdr_plan_rows):
            if i < len(plan):
                sv.set(plan[i].get("start", ""))
                mv.set(SDR_MODE_LABELS.get(plan[i].get("mode"), plan[i].get("mode", "")))
            else:
                sv.set("")
                mv.set("")
        self.sdr_hold_var.set(str(sched.get("hold_minutes", 15)))
        self._sdr_sched_show()

    def _sdr_save_schedule(self):
        rotation = []
        for mv, nv in self.sdr_rot_rows:
            mode = self._sdr_label_to_mode.get(mv.get())
            try:
                minutes = int(nv.get())
            except ValueError:
                continue
            if mode:
                rotation.append([mode, minutes])
        planning = []
        for sv, mv in self.sdr_plan_rows:
            mode = self._sdr_label_to_mode.get(mv.get())
            if mode and re.match(r"^\d{1,2}:\d{2}$", sv.get().strip()):
                planning.append({"start": sv.get().strip(), "mode": mode})
        t = self.sdr_sched_type.get()
        if t == "rotation" and not rotation:
            messagebox.showwarning("Rotation", "Indiquez au moins un module et sa durée.")
            return
        if t == "planning" and not planning:
            messagebox.showwarning("Planning", "Indiquez au moins un créneau (heure HH:MM et module).")
            return
        try:
            hold = int(self.sdr_hold_var.get())
        except ValueError:
            hold = 15
        payload = {"type": t, "hold_minutes": hold}
        if rotation:
            payload["rotation"] = rotation
        if planning:
            payload["planning"] = planning
        host = self.config_data.get("pi_host", "192.168.1.229")
        port = self.config_data.get("port_sdr_switch", 5006)
        self.sdr_sched_status.config(text="Enregistrement…", fg=theme.MUTED)

        def worker():
            try:
                data = api_client.set_sdr_schedule(host, port, payload)
                err = None
            except Exception as exc:
                data, err = None, str(exc)
            self.after(0, done, data, err)

        def done(data, err):
            if err:
                self.sdr_sched_status.config(
                    text="Échec : le service de bascule du Pi est-il à jour ?", fg=theme.RED,
                )
                return
            self.sdr_sched_status.config(text=f"Enregistré à {time.strftime('%H:%M:%S')}", fg=theme.GREEN)
            self._sdr_sched_loaded = False
            self._render_sdr_mode(data, None)

        threading.Thread(target=worker, daemon=True).start()

    def _render_sdr_control(self, data):
        if not hasattr(self, "sdr_mode_value"):
            return
        mode = data.get("mode")
        active = (data.get("active") or {}).get(mode)
        sched = data.get("schedule") or {}
        if mode == "off":
            self.sdr_mode_value.config(text="Arrêtée (aucun module)", fg=theme.RED)
            parts = ["tous les modules sont arrêtés", "partage automatique suspendu"]
        else:
            self.sdr_mode_value.config(
                text=SDR_MODE_LABELS.get(mode, mode or "—"), fg=theme.ACCENT if active else theme.ORANGE,
            )
            parts = ["service en marche" if active else "service arrêté ou en échec (clé débranchée ?)"]
        names = {"manual": "manuel", "rotation": "rotation", "planning": "planning horaire"}
        if mode != "off":
            parts.append(f"partage : {names.get(sched.get('type'), '—')}")
        hold = data.get("hold_until")
        if hold and hold > time.time():
            parts.append(f"pause jusqu'à {time.strftime('%H:%M', time.localtime(hold))}")
        nxt = data.get("next_change")
        if nxt and sched.get("type") != "manual" and mode != "off":
            parts.append(f"prochain changement vers {time.strftime('%H:%M', time.localtime(nxt))}")
        self.sdr_mode_detail.config(text="   ·   ".join(parts))
        installed = data.get("units") or {}
        for m, b in self.sdr_mode_buttons.items():
            if not installed.get(m):
                b.config(state="disabled", bg=theme.BG_CARD_ALT, fg=theme.MUTED, text=f"{SDR_MODE_LABELS[m]} (absent)")
            elif m == mode:
                b.config(state="normal", bg=theme.ACCENT, fg="#1a1200", text=SDR_MODE_LABELS[m])
            else:
                b.config(state="normal", bg=theme.BG_CARD_ALT, fg=theme.TEXT, text=SDR_MODE_LABELS[m])
        if not self._sdr_sched_loaded and sched:
            self._sdr_fill_schedule(sched)
            self._sdr_sched_loaded = True
        self.sdr_stop_button.config(
            state="disabled" if mode == "off" else "normal",
            bg=theme.BG_CARD_ALT if mode == "off" else theme.RED,
            fg=theme.MUTED if mode == "off" else "#0b0f17",
        )
        if "sdr" in self.tile_chips:
            self.tile_chips["sdr"].config(
                text="Clé arrêtée" if mode == "off"
                else f"Clé sur {SDR_MODE_LABELS.get(mode, mode)} · {names.get(sched.get('type'), '—')}",
            )

    # ---------------------------------------------------- Vignettes accueil --

    def _refresh_all_tiles(self):
        mode = getattr(self, "_sdr_mode", None)
        if mode and mode != "off":
            self._sdr_last_mode = mode
        self._render_home_key()
        for chip in getattr(self, "tile_chips", {}).values():
            if isinstance(chip, _TileChip):
                chip.refresh()

    # ------------------------------------------------------------- Santé --

    def _build_health_tab(self, frame):
        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        power_outer = theme.card(wrapper)
        power_outer.pack(fill="x", padx=12, pady=(8, 8))
        p = power_outer.inner
        p.configure(padding=16)
        tk.Label(p, text="ALIMENTATION", bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.health_power_value = tk.Label(p, text="—", bg=theme.BG_CARD, fg=theme.TEXT, font=("Segoe UI", 20, "bold"))
        self.health_power_value.pack(anchor="w", pady=(4, 2))
        self.health_power_detail = tk.Label(
            p, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY, justify="left", wraplength=1200,
        )
        self.health_power_detail.pack(anchor="w")

        grid = tk.Frame(wrapper, bg=theme.BG)
        grid.pack(fill="x", padx=6)
        self.health_tiles = {}
        specs = [
            ("temp", "TEMPÉRATURE CPU"), ("load", "CHARGE CPU"), ("mem", "MÉMOIRE"), ("disk", "DISQUE"),
            ("uptime", "EN MARCHE DEPUIS"), ("sdr", "CLÉS SDR DÉTECTÉES"),
        ]
        for i, (key, title) in enumerate(specs):
            r, c = divmod(i, 3)
            grid.grid_columnconfigure(c, weight=1, uniform="h")
            outer = theme.card(grid)
            outer.grid(row=r, column=c, sticky="nsew", padx=6, pady=(0, 8))
            inner = outer.inner
            inner.configure(padding=14)
            tk.Label(inner, text=title, bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
            v = tk.Label(inner, text="—", bg=theme.BG_CARD, fg=theme.TEXT, font=("Segoe UI", 18, "bold"))
            v.pack(anchor="w", pady=(4, 0))
            s = tk.Label(inner, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
            s.pack(anchor="w")
            self.health_tiles[key] = (v, s)

        self.health_sys_label = tk.Label(wrapper, text="", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.health_sys_label.pack(anchor="w", padx=14, pady=(4, 8))

        # --- Maintenance
        m_outer = theme.card(wrapper)
        m_outer.pack(fill="x", padx=12, pady=(0, 12))
        m = m_outer.inner
        m.configure(padding=14)
        tk.Label(m, text="Maintenance", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION).grid(row=0, column=0, columnspan=6, sticky="w")
        ttk.Button(m, text="Sauvegarder la configuration…", style="Accent.TButton",
                   command=self._backup_config).grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Button(m, text="Redémarrer le Pi", style="Flat.TButton",
                   command=lambda: self._power_action("reboot")).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(10, 0))
        ttk.Button(m, text="Éteindre le Pi", style="Flat.TButton",
                   command=lambda: self._power_action("shutdown")).grid(row=1, column=2, sticky="w", padx=(10, 0), pady=(10, 0))
        tk.Label(m, text="Journal d'un service :", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_BODY).grid(row=1, column=3, sticky="e", padx=(30, 6), pady=(10, 0))
        self._log_units = {
            "ADS-B (readsb)": "readsb", "APRS (Direwolf)": "direwolf", "AIS (AIS-catcher)": "ais-catcher",
            "SvxLink": "svxlink", "Winlink (Pat)": "pat@f4jcf", "OpenWebRX": "docker:openwebrx",
            "API APRS": "aprs-api", "API SvxLink": "svxlink-api", "Bascule de clé": "sdr-switch",
            "Surveillance / notifications": "station-monitor",
        }
        self.health_log_var = tk.StringVar(value="ADS-B (readsb)")
        ttk.Combobox(m, textvariable=self.health_log_var, values=list(self._log_units), state="readonly",
                     width=26).grid(row=1, column=4, sticky="w", pady=(10, 0))
        ttk.Button(m, text="Afficher", style="Flat.TButton", command=self._show_service_log).grid(
            row=1, column=5, sticky="w", padx=(6, 0), pady=(10, 0))
        upd_row = tk.Frame(m, bg=theme.BG_CARD)
        upd_row.grid(row=2, column=0, columnspan=6, sticky="w", pady=(10, 0))
        self.health_update_btn = ttk.Button(upd_row, text="Mettre à jour le Pi", style="Accent.TButton",
                                            command=lambda: self._update_pi(ask=True))
        self.health_update_btn.pack(side="left")
        self.health_version_label = tk.Label(upd_row, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY,
                                             justify="left", wraplength=1100)
        self.health_version_label.pack(side="left", padx=(10, 0))
        self.health_maint_status = tk.Label(m, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE,
                                            justify="left", wraplength=1300)
        self.health_maint_status.grid(row=3, column=0, columnspan=6, sticky="w", pady=(8, 0))

    def _pi_endpoint(self):
        return self.config_data.get("pi_host", "192.168.1.229"), self.config_data.get("port_sdr_switch", 5006)

    def _render_pi_version(self, data):
        """Compare la version des scripts du Pi à celle de l'appli, et
        propose une fois par session de mettre le Pi à jour."""
        if not hasattr(self, "health_version_label"):
            return
        app_v = f"v{__version__}"
        if "pi_version" not in data:
            self.health_version_label.config(
                text="Le Pi utilise une ancienne version du service de bascule : une dernière mise à jour par SSH "
                     "est nécessaire (commande donnée dans la conversation), ensuite ce bouton suffira.",
                fg=theme.ORANGE)
            return
        pi_v = data.get("pi_version")
        if pi_v == app_v:
            self.health_version_label.config(text=f"Pi à jour ({pi_v}).", fg=theme.GREEN)
            return
        self.health_version_label.config(
            text=f"Scripts du Pi : {pi_v or 'version inconnue'}  ·  appli : {app_v}  →  mise à jour conseillée.",
            fg=theme.ORANGE)
        if not getattr(self, "_pi_update_prompted", False) and not getattr(self, "_pi_updating", False):
            self._pi_update_prompted = True
            self.after(1500, lambda: self._update_pi(ask=True, auto=True, pi_v=pi_v))

    def _update_pi(self, ask=True, auto=False, pi_v=None):
        app_v = f"v{__version__}"
        if getattr(self, "_pi_updating", False):
            return
        if ask:
            intro = (f"Les scripts du Raspberry Pi ({pi_v or 'version inconnue'}) ne correspondent pas à cette "
                     f"version de l'appli ({app_v}).\n\n") if auto else ""
            if not messagebox.askyesno(
                "Mettre à jour le Pi",
                intro + f"Installer sur le Pi les scripts de la version {app_v} ?\n\nLes services du Pi "
                "redémarrent quelques secondes ; vos réglages sont conservés.",
            ):
                return
        host, port = self._pi_endpoint()
        self._pi_updating = True
        self.health_update_btn.config(state="disabled")
        self.health_maint_status.config(text=f"Mise à jour du Pi vers {app_v}…", fg=theme.MUTED)

        def worker():
            try:
                api_client.pi_update(host, port, app_v)
            except Exception as exc:
                self.after(0, finish, False, f"Échec du lancement : {exc}")
                return
            deadline = time.time() + 120
            last_log = ""
            while time.time() < deadline:
                time.sleep(2)
                try:
                    st = api_client.get_pi_update_status(host, port)
                except Exception:
                    continue  # le service redémarre : on réessaie
                last_log = "\n".join(st.get("log") or [])
                self.after(0, lambda t=last_log: self.health_maint_status.config(text=t, fg=theme.MUTED))
                res = st.get("result")
                if res and not st.get("running"):
                    if res.get("ok"):
                        # Attendre que le service redémarré réponde avec la nouvelle version.
                        for _ in range(15):
                            time.sleep(2)
                            try:
                                if api_client.get_pi_update_status(host, port).get("pi_version") == app_v:
                                    break
                            except Exception:
                                pass
                        self.after(0, finish, True, f"Pi mis à jour en {app_v}.")
                    else:
                        self.after(0, finish, False, f"Échec : {res.get('error')}\n{last_log}")
                    return
            self.after(0, finish, False, "Pas de réponse du Pi après 2 minutes.\n" + last_log)

        def finish(ok, msg):
            self._pi_updating = False
            self.health_update_btn.config(state="normal")
            self.health_maint_status.config(text=msg, fg=theme.GREEN if ok else theme.RED)
            if ok:
                self.health_version_label.config(text=f"Pi à jour ({app_v}).", fg=theme.GREEN)
                messagebox.showinfo("Mise à jour du Pi", msg)
            threading.Thread(target=self._refresh_once, daemon=True).start()

        threading.Thread(target=worker, daemon=True).start()

    def _power_action(self, action):
        verb = "redémarrer" if action == "reboot" else "éteindre"
        extra = ("" if action == "reboot" else
                 "\n\nAttention : une fois éteint, le Pi ne pourra être rallumé qu'en débranchant puis "
                 "rebranchant son alimentation.")
        if not messagebox.askyesno(f"{verb.capitalize()} le Pi", f"Voulez-vous vraiment {verb} le Raspberry Pi ?{extra}"):
            return
        host, port = self._pi_endpoint()

        def worker():
            try:
                api_client.pi_power_action(host, port, action)
                msg, col = (f"Demande envoyée à {time.strftime('%H:%M:%S')} : le Pi "
                            + ("redémarre (environ 1 à 2 minutes)." if action == "reboot" else "s'éteint.")), theme.ORANGE
            except Exception as exc:
                msg, col = f"Échec : {exc}", theme.RED
            self.after(0, lambda: self.health_maint_status.config(text=msg, fg=col))

        threading.Thread(target=worker, daemon=True).start()

    def _backup_config(self):
        path = filedialog.asksaveasfilename(
            title="Enregistrer la sauvegarde", defaultextension=".tar.gz",
            initialfile=f"station-sdr-config-{time.strftime('%Y%m%d-%H%M')}.tar.gz",
            filetypes=[("Archive", "*.tar.gz"), ("Tous les fichiers", "*.*")],
        )
        if not path:
            return
        host, port = self._pi_endpoint()
        self.health_maint_status.config(text="Sauvegarde en cours…", fg=theme.MUTED)

        def worker():
            try:
                data = api_client.get_pi_backup(host, port)
                with open(path, "wb") as f:
                    f.write(data)
                msg, col = (f"Sauvegarde enregistrée ({len(data) // 1024} Ko) : {path}  —  contient des mots de "
                            "passe (Winlink, EchoLink), à garder en lieu sûr."), theme.GREEN
            except Exception as exc:
                msg, col = f"Échec de la sauvegarde : {exc}", theme.RED
            self.after(0, lambda: self.health_maint_status.config(text=msg, fg=col))

        threading.Thread(target=worker, daemon=True).start()

    def _show_service_log(self):
        label = self.health_log_var.get()
        unit = self._log_units.get(label)
        host, port = self._pi_endpoint()

        def worker():
            try:
                text = api_client.get_service_log(host, port, unit, 120).get("text", "")
            except Exception as exc:
                text = f"Lecture impossible : {exc}\n(Le service de bascule du Pi est-il à jour ?)"
            self.after(0, show, text)

        def show(text):
            win = tk.Toplevel(self)
            win.title(f"Journal — {label}")
            win.configure(bg=theme.BG)
            win.geometry("1100x600")
            bar = tk.Frame(win, bg=theme.BG)
            bar.pack(fill="x", padx=10, pady=(10, 0))
            tk.Label(bar, text=f"{label} — 120 dernières lignes ({time.strftime('%H:%M:%S')})", bg=theme.BG,
                     fg=theme.TEXT, font=theme.FONT_SECTION).pack(side="left")
            txt = tk.Text(win, bg=theme.BG_CARD_ALT, fg=theme.TEXT, wrap="none", font=("Consolas", 9),
                          relief="flat", padx=8, pady=8)
            ysb = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
            txt.configure(yscrollcommand=ysb.set)
            ysb.pack(side="right", fill="y", pady=10)
            txt.pack(fill="both", expand=True, padx=(10, 0), pady=10)
            txt.tag_configure("err", foreground=theme.RED)
            txt.tag_configure("warn", foreground=theme.ORANGE)
            for line in (text or "(journal vide)").splitlines():
                low = line.lower()
                tag = "err" if any(k in low for k in ("error", "erreur", "fatal", "failed", "échec")) else \
                      "warn" if any(k in low for k in ("warn", "attention", "distortion")) else ""
                txt.insert("end", line + "\n", tag)
            txt.see("end")
            txt.config(state="disabled")

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _fmt_bytes(n):
        if n is None:
            return "—"
        for unit in ("o", "Ko", "Mo", "Go", "To"):
            if n < 1024 or unit == "To":
                return f"{n:.1f} {unit}" if unit != "o" else f"{n} o"
            n /= 1024

    def _render_health(self, data, error):
        if not hasattr(self, "health_tiles"):
            return
        self._render_home_gauges(None if error else data)
        if error:
            self.chip_health.config(text="Pi : —", bg=theme.BG_CARD_ALT, fg=theme.TEXT)
            self.health_power_value.config(text="Indisponible", fg=theme.MUTED)
            self.health_power_detail.config(
                text="Le service de bascule du Pi (sdr-switch, port 5006) ne répond pas ou n'est pas à jour.",
            )
            if "health" in self.tile_chips:
                self.tile_chips["health"].config(text="—")
            return

        th = data.get("throttled") or {}
        temp = data.get("cpu_temp_c")
        if th.get("under_voltage_now"):
            power_txt, power_col = "SOUS-TENSION EN COURS", theme.RED
            detail = ("Le Pi manque de courant en ce moment : les clés USB (SDR) peuvent décrocher à tout instant. "
                      "Solution : hub USB alimenté pour la clé SDR et/ou alimentation officielle 5,1 V / 2,5 A "
                      "avec un câble court.")
        elif th.get("under_voltage_occurred"):
            power_txt, power_col = "Sous-tension depuis le démarrage", theme.ORANGE
            detail = ("La tension est correcte maintenant, mais elle a chuté au moins une fois depuis le dernier "
                      "démarrage du Pi (le compteur ne se remet à zéro qu'au redémarrage).")
        elif th:
            power_txt, power_col = "Correcte", theme.GREEN
            detail = "Aucune sous-tension depuis le dernier démarrage."
        else:
            power_txt, power_col = "Inconnue", theme.MUTED
            detail = "vcgencmd indisponible sur le Pi."
        extras = []
        if th.get("throttled_now"):
            extras.append("processeur bridé en ce moment")
        elif th.get("throttled_occurred"):
            extras.append("processeur bridé au moins une fois")
        if th.get("temp_limit_now"):
            extras.append("limite de température atteinte")
        if extras:
            detail += "   ·   " + ", ".join(extras).capitalize() + "."
        if data.get("throttled_raw"):
            detail += f"   (code {data['throttled_raw']})"
        self.health_power_value.config(text=power_txt, fg=power_col)
        self.health_power_detail.config(text=detail)

        v, s = self.health_tiles["temp"]
        if temp is None:
            v.config(text="—", fg=theme.MUTED)
        else:
            v.config(text=f"{temp:.1f} °C", fg=theme.RED if temp >= 80 else theme.ORANGE if temp >= 70 else theme.GREEN)
        s.config(text="bridage à partir de 80 °C" if temp is not None else "")

        v, s = self.health_tiles["load"]
        load, ncpu = data.get("load"), data.get("cpu_count") or 1
        if load:
            pct = load[0] / ncpu * 100
            v.config(text=f"{pct:.0f} %", fg=theme.RED if pct >= 90 else theme.ORANGE if pct >= 70 else theme.TEXT)
            s.config(text=f"moyenne 1 / 5 / 15 min : {load[0]} / {load[1]} / {load[2]} ({ncpu} cœurs)")

        v, s = self.health_tiles["mem"]
        tot, av = data.get("mem_total"), data.get("mem_available")
        if tot and av is not None:
            pct = (tot - av) / tot * 100
            v.config(text=f"{pct:.0f} % utilisée", fg=theme.ORANGE if pct >= 85 else theme.TEXT)
            s.config(text=f"{self._fmt_bytes(av)} libres sur {self._fmt_bytes(tot)}")

        v, s = self.health_tiles["disk"]
        tot, free = data.get("disk_total"), data.get("disk_free")
        if tot and free is not None:
            pct = (tot - free) / tot * 100
            v.config(text=f"{pct:.0f} % utilisé", fg=theme.RED if pct >= 95 else theme.ORANGE if pct >= 85 else theme.TEXT)
            s.config(text=f"{self._fmt_bytes(free)} libres sur {self._fmt_bytes(tot)}")

        v, s = self.health_tiles["uptime"]
        up = data.get("uptime_s")
        if up is not None:
            d, rem = divmod(up, 86400)
            h, rem = divmod(rem, 3600)
            v.config(text=(f"{d} j {h} h" if d else f"{h} h {rem // 60:02d} min"), fg=theme.TEXT)
            s.config(text=f"démarré le {time.strftime('%d/%m à %H:%M', time.localtime(time.time() - up))}")

        v, s = self.health_tiles["sdr"]
        n = data.get("sdr_dongles")
        if n is not None:
            v.config(text=str(n), fg=theme.RED if n == 0 else theme.TEXT)
            s.config(text="aucune clé visible sur l'USB !" if n == 0 else "clé(s) RTL-SDR sur l'USB")

        self.health_sys_label.config(
            text=f"{data.get('model') or 'Raspberry Pi'}   ·   {data.get('hostname', '')}   ·   noyau {data.get('kernel', '')}",
        )

        # Bandeau + vignette
        if th.get("under_voltage_now") or (temp is not None and temp >= 80) or n == 0:
            chip_txt, bg, fg = "⚠ Pi : " + ("sous-tension" if th.get("under_voltage_now") else
                                            "clé SDR absente" if n == 0 else "surchauffe"), theme.RED, "#0b0f17"
        elif th.get("under_voltage_occurred") or (temp is not None and temp >= 70):
            chip_txt, bg, fg = "Pi : à surveiller", theme.ORANGE, "#0b0f17"
        else:
            chip_txt, bg, fg = "Pi : OK", theme.BG_CARD_ALT, theme.TEXT
        self.chip_health.config(text=f" {chip_txt} ", bg=bg, fg=fg)
        if "health" in self.tile_chips:
            tile = power_txt if power_col != theme.GREEN else "Alimentation OK"
            if temp is not None:
                tile += f" · {temp:.0f} °C"
            self.tile_chips["health"].config(text=tile)

    # ------------------------------------------------------- Paramètres --

    def _open_settings(self):
        host = simpledialog.askstring(
            "Paramètres", "Adresse IP du Raspberry Pi :",
            initialvalue=self.config_data.get("pi_host", ""), parent=self,
        )
        if host:
            self.config_data["pi_host"] = host.strip()
            self.chip_host.config(text=f"Hôte {self.config_data['pi_host']}")

        token = simpledialog.askstring(
            "Paramètres",
            "Jeton GitHub (optionnel, pour la mise à jour automatique\n"
            "du dépôt privé F4JCF/STATION-SDR) :",
            initialvalue=self.config_data.get("github_token", ""), parent=self, show="*",
        )
        if token is not None:
            self.config_data["github_token"] = token.strip()

        save_config(self.config_data)
        messagebox.showinfo("Paramètres", "Paramètres enregistrés.")

    # --------------------------------------------------------- Updater --

    def _check_previous_update_failure(self):
        """Si une mise à jour précédente n'a pas réussi à remplacer
        l'exécutable (antivirus, dossier protégé en écriture...), le
        script de relancement laisse un marqueur plutôt que d'échouer en
        silence — on le signale ici, une seule fois."""
        details = updater.read_and_clear_failure_marker()
        if not details:
            return
        messagebox.showwarning(
            "Mise à jour précédente incomplète",
            "La dernière mise à jour s'est fermée et relancée, mais n'a pas réussi à "
            "remplacer le fichier exécutable (l'application a donc redémarré dans son "
            "ancienne version) :\n\n"
            f"{details.strip()}\n\n"
            "Causes possibles : antivirus qui bloque/retient le nouveau fichier, ou "
            "dossier d'installation protégé en écriture. En attendant, vous pouvez "
            "télécharger et remplacer l'exécutable manuellement depuis :\n"
            "https://github.com/F4JCF/STATION-SDR/releases/latest",
        )
        self._check_crash_log()

    def _check_crash_log(self):
        """Signale une seule fois un plantage (démarrage précédent ou
        callback) déjà enregistré dans CRASH_LOG_PATH, sans jamais
        re-notifier le même événement en boucle à chaque démarrage. On ne
        supprime pas le fichier (utile pour du support), on retient juste
        la date de modification déjà signalée dans la configuration."""
        try:
            mtime = os.path.getmtime(CRASH_LOG_PATH)
        except OSError:
            return
        last_notified = self.config_data.get("_last_crash_notified_mtime", 0)
        if mtime <= last_notified:
            return
        self.config_data["_last_crash_notified_mtime"] = mtime
        save_config(self.config_data)
        messagebox.showwarning(
            "Erreur détectée lors d'un démarrage précédent",
            "L'application a rencontré une erreur imprévue lors d'une précédente "
            "ouverture (par exemple juste après une mise à jour) et a dû se fermer.\n\n"
            f"Détails complets enregistrés dans :\n{CRASH_LOG_PATH}\n\n"
            "Si le problème se reproduit, ce fichier aidera à comprendre la cause.",
        )

    def _check_update_on_startup(self):
        if not self.config_data.get("check_updates", True):
            return
        threading.Thread(target=self._run_update_check, kwargs={"silent": True}, daemon=True).start()

    def _manual_update_check(self):
        threading.Thread(target=self._run_update_check, kwargs={"silent": False}, daemon=True).start()

    def _run_update_check(self, silent: bool):
        token = self.config_data.get("github_token", "")
        try:
            info = updater.check_for_update(token)
        except updater.PrivateRepoAuthRequired as exc:
            self.after(0, self._prompt_missing_token, str(exc))
            return
        except requests.RequestException as exc:
            if not silent:
                self.after(0, messagebox.showerror, "Mise à jour",
                            f"Impossible de vérifier les mises à jour :\n{exc}")
            return

        if not info:
            if not silent:
                self.after(0, messagebox.showinfo, "Mise à jour", "Vous utilisez déjà la dernière version.")
            return

        self.after(0, self._prompt_update, info, token)

    def _prompt_missing_token(self, message: str):
        answer = messagebox.askyesno("Mise à jour", f"{message}\n\nOuvrir les Paramètres maintenant ?")
        if answer:
            self._open_settings()

    def _prompt_update(self, info: dict, token: str):
        answer = messagebox.askyesno(
            "Mise à jour disponible",
            f"Une nouvelle version est disponible : {info['tag']}\n\n"
            f"{info.get('notes', '')}\n\nInstaller maintenant ?",
        )
        if not answer:
            return
        threading.Thread(target=self._download_and_apply_update, args=(info, token), daemon=True).start()

    def _download_and_apply_update(self, info: dict, token: str):
        import os
        import tempfile

        dest = os.path.join(tempfile.gettempdir(), info["asset_name"])
        try:
            updater.download_asset(info["asset_url"], token, dest)
        except requests.RequestException as exc:
            self.after(0, messagebox.showerror, "Mise à jour",
                        f"Échec du téléchargement :\n{exc}")
            return
        self.after(0, self._finish_update, dest)

    def _finish_update(self, dest: str):
        # Le relancement se fait via un script batch détaché (Windows ne
        # permet pas de remplacer un .exe en cours d'exécution) : on
        # prévient l'utilisateur avant de fermer, sinon la fenêtre qui se
        # ferme puis se rouvre un instant plus tard peut sembler être un
        # plantage plutôt qu'une mise à jour.
        messagebox.showinfo(
            "Mise à jour",
            "Téléchargement terminé. L'application va se fermer puis "
            "redémarrer automatiquement avec la nouvelle version.",
        )
        try:
            updater.apply_update_and_restart(dest)
        except Exception as exc:
            messagebox.showerror(
                "Mise à jour",
                "Le redémarrage automatique a échoué :\n"
                f"{exc}\n\n"
                "Fermez l'application et relancez-la manuellement pour "
                "terminer l'installation.",
            )

    # -------------------------------------------------------------- Fin --

    def _on_close(self):
        self._stop_event.set()
        if getattr(self, "_tray_icon", None) is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self.destroy()

    def _on_callback_exception(self, exc_type, exc_value, exc_tb):
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _log_crash(tb_text, context="callback")


App.SDR_LABELS = SDR_MODE_LABELS


def main():
    # Avec --windowed (pas de console), une exception non rattrapée au
    # démarrage se termine en silence : la fenêtre ne s'ouvre jamais (ou
    # se ferme instantanément) sans qu'on en sache la raison. C'est
    # exactement le symptôme observé après un redémarrage automatique qui
    # tombe sur une nouvelle version buguée — ce filet de sécurité rend
    # l'erreur visible (boîte de dialogue + journal) au lieu de laisser
    # l'exécutable disparaître sans aucune trace.
    try:
        app = App()
        app.mainloop()
    except Exception:
        _handle_startup_crash()


def _handle_startup_crash():
    tb = traceback.format_exc()
    _log_crash(tb, context="démarrage")

    try:
        error_root = tk.Tk()
        error_root.withdraw()
        last_line = tb.strip().splitlines()[-1] if tb.strip() else "Erreur inconnue"
        messagebox.showerror(
            "STATION-SDR — erreur au démarrage",
            "L'application a rencontré une erreur au démarrage et n'a pas pu s'ouvrir "
            "correctement.\n\n"
            f"{last_line}\n\n"
            f"Détails complets enregistrés dans :\n{CRASH_LOG_PATH}",
        )
        error_root.destroy()
    except Exception:
        pass  # même l'affichage de l'erreur a échoué : le journal reste la seule trace


if __name__ == "__main__":
    main()
