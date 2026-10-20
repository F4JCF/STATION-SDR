"""STATION-SDR - Tableau de bord Windows pour le Raspberry Pi F4JCF-SDR-STATION.

Interface Tkinter : bandeau d'en-tête, puces de statut global, écran
d'accueil en vignettes cliquables (météo, trafic aérien, APRS, Winlink,
récepteurs SDR, services) ouvrant chacune un module dédié, rafraîchis
périodiquement en arrière-plan, plus mise à jour automatique au démarrage.
"""

import io
import math
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
from tkinter import messagebox, simpledialog, ttk

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
    ("aprs", "APRS", "Direwolf — réception de paquets"),
    ("ais", "AIS", "Trafic maritime"),
    ("winlink", "Winlink", "Messagerie radio par paquet"),
    ("sdr", "Clé SDR", "Pilotage et partage automatique"),
    ("svxlink", "SvxLink", "Relais vocal / Echolink"),
    ("openwebrx", "OpenWebRX", "Réception SDR (ouvre le navigateur)"),
    ("services", "Services système", "Vue d'ensemble des services"),
    ("health", "Santé du Pi", "Alimentation, température, ressources"),
]

# Module -> mode de la clé SDR unique (sdr_switch.py sur le Pi). Ouvrir
# l'une de ces pages bascule la clé vers ce module, ce qui arrête
# automatiquement le module qui l'utilisait.
MODULE_SDR_MODE = {
    "aircraft": "adsb",
    "ais": "ais",
    "aprs": "aprs",
    "openwebrx": "openwebrx",
}
SDR_MODE_LABELS = {"adsb": "ADS-B", "ais": "AIS", "aprs": "APRS", "openwebrx": "OpenWebRX"}

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


def _alt_color(alt_ft):
    """Couleur par tranche d'altitude, dans l'esprit du dégradé de tar1090."""
    try:
        alt = float(alt_ft)
    except (TypeError, ValueError):
        return "#8b96a5"  # altitude inconnue / au sol
    if alt < 5000:
        return "#f85149"
    if alt < 15000:
        return "#d29922"
    if alt < 25000:
        return "#3fb950"
    if alt < 35000:
        return "#4aa3ff"
    return "#b15bff"


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


def _plane_icon(alt_ft, track, size=22):
    """Icône avion (triangle simple) colorée par altitude et orientée selon
    le cap. Mise en cache par (couleur, cap arrondi) pour éviter de
    regénérer une image à chaque rafraîchissement."""
    color = _alt_color(alt_ft)
    try:
        heading = round(float(track)) % 360
    except (TypeError, ValueError):
        heading = 0
    cache_key = (color, heading)
    cached = _PLANE_ICON_CACHE.get(cache_key)
    if cached is not None:
        return cached

    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base)
    cx = size / 2
    draw.polygon(
        [(cx, 1), (size - 2, size - 4), (cx, size - 9), (2, size - 4)],
        fill=color, outline="#0b0f17",
    )
    rotated = base.rotate(-heading, resample=Image.BICUBIC, expand=False)
    photo = ImageTk.PhotoImage(rotated)
    _PLANE_ICON_CACHE[cache_key] = photo
    return photo


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
        if mode and current and mode != current:
            self.label.config(
                text=f" En pause · clé sur {SDR_MODE_LABELS.get(current, current)} ", fg=theme.MUTED,
                bg=theme.BG_CARD_ALT,
            )
            self.frame.config(bg=theme.BORDER)
        elif mode and current == mode:
            self.label.config(text=f" ● {self.text} ", fg="#1a1200", bg=theme.ACCENT)
            self.frame.config(bg=theme.ACCENT)
        else:
            self.label.config(text=f" {self.text} ", fg=theme.TEXT, bg=theme.BG_CARD_ALT)
            self.frame.config(bg=theme.BORDER)


class App(tk.Tk):
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
        self._build_menu()
        self._build_header()
        self._build_body()
        self._tick_clock()

        self.after(100, self._check_previous_update_failure)
        self.after(200, self._check_update_on_startup)
        self._start_refresh_loop()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- UI --

    def _build_menu(self):
        menubar = tk.Menu(self)
        app_menu = tk.Menu(menubar, tearoff=0)
        app_menu.add_command(label="Paramètres...", command=self._open_settings)
        app_menu.add_command(label="Vérifier les mises à jour", command=self._manual_update_check)
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
            logo_img = Image.open(_resource_path("assets/logo.png")).convert("RGBA")
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
        self._build_info_tab(
            self.pages["winlink"], "Winlink", "winlink",
            "Aucune passerelle Winlink (par ex. Pat Winlink) n'est installée sur\n"
            "F4JCF-SDR-STATION pour l'instant. Winlink permet d'échanger des emails\n"
            "par radio (paquet, VARA, Ardop) via un réseau de stations relais.\n\n"
            "Pour l'intégrer ici, il faudrait installer un client Winlink sur le\n"
            "Raspberry et exposer son état (connecté, file d'attente, dernière\n"
            "synchronisation) via une petite API, comme pour les autres services.",
        )
        self._build_ais_tab(self.pages["ais"])
        self._build_svxlink_tab(self.pages["svxlink"])
        self._build_sdr_tab(self.pages["sdr"])
        self._build_health_tab(self.pages["health"])

        self.show_home()

    def _build_home_tiles(self):
        grid = tk.Frame(self.home_frame, bg=theme.BG)
        grid.pack(fill="both", expand=True, padx=4, pady=4)

        self.tile_chips = {}
        columns = 5
        self.tile_frames = {}
        for i, (key, title, subtitle) in enumerate(HOME_TILES):
            r, c = divmod(i, columns)
            grid.grid_columnconfigure(c, weight=1)
            outer = theme.card(grid)
            outer.grid(row=r, column=c, sticky="nsew", padx=10, pady=10)
            inner = outer.inner
            inner.configure(padding=18)

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
                wraplength=220, justify="left",
            )
            subtitle_label.pack(anchor="w", pady=(4, 10))
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
            return
        self._sdr_switching = True
        label = SDR_MODE_LABELS.get(mode, mode)
        self.chip_sdr.config(text=f"Clé SDR → {label}…")
        host = self.config_data.get("pi_host", "192.168.1.229")
        port = self.config_data.get("port_sdr_switch", 5006)

        def worker():
            try:
                result = api_client.set_sdr_mode(host, port, mode)
                error = None if result.get("ok", True) else "échec du démarrage"
            except Exception as exc:
                result, error = None, str(exc)
            self.after(0, done, error)

        def done(error):
            self._sdr_switching = False
            if error is None:
                self._sdr_mode = mode
                self.chip_sdr.config(text=f"Clé SDR : {label}")
                self._refresh_all_tiles()
            else:
                self.chip_sdr.config(text="Clé SDR : bascule indisponible")
            if then:
                # Laisse le temps au service web (OpenWebRX) de démarrer.
                self.after(4000 if error is None else 0, then)
            # Rafraîchit tout de suite pour refléter le nouveau service.
            threading.Thread(target=self._refresh_once, daemon=True).start()

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
        self._fetch(lambda: api_client.get_ais_ships(host, cfg.get("port_ais", 8100)), self._render_ais)
        self._fetch(
            lambda: api_client.get_sdr_mode(host, cfg.get("port_sdr_switch", 5006)), self._render_sdr_mode,
        )
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

        map_outer = theme.card(wrapper)
        map_outer.pack(side="left", fill="both", expand=True, pady=12, padx=(0, 12))
        self.weather_map = TkinterMapView(map_outer.inner, corner_radius=0)
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

        # Même disposition que la page Météo : colonne de gauche (liste +
        # fiche détaillée, largeur naturelle) et carte à droite occupant
        # tout le reste en largeur ET en hauteur.
        wrapper = tk.Frame(outer_wrapper, bg=theme.BG)
        wrapper.pack(side="left", fill="y", pady=(0, 6))

        top_row = tk.Frame(wrapper, bg=theme.BG)
        top_row.pack(fill="x", padx=12, pady=(8, 6))
        self.aircraft_count_label = tk.Label(
            top_row, text="Avions détectés : —", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION,
        )
        self.aircraft_count_label.pack(side="left")

        ttk.Button(
            top_row, text="Carte web tar1090", style="Flat.TButton",
            command=lambda: self._open_url(
                f"http://{self.config_data.get('pi_host', '192.168.1.229')}:{self.config_data.get('port_adsb', 8504)}/"
            ),
        ).pack(side="right", padx=(8, 0))

        search_box = tk.Frame(top_row, bg=theme.BG)
        search_box.pack(side="right")
        tk.Label(
            search_box, text="Rechercher (indicatif / hex) :", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE,
        ).pack(side="left", padx=(0, 6))
        self.aircraft_search_var = tk.StringVar()
        self.aircraft_search_var.trace_add("write", lambda *_a: self._repaint_aircraft())
        ttk.Entry(search_box, textvariable=self.aircraft_search_var, width=14).pack(side="left")

        table_outer = theme.card(wrapper)
        table_outer.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        columns = ("flight", "hex", "alt", "gs", "track", "squawk")
        self.aircraft_tree = ttk.Treeview(table_outer.inner, columns=columns, show="headings", height=8)
        headings = {
            "flight": "Indicatif", "hex": "Hex", "alt": "Altitude (ft)",
            "gs": "Vitesse (kt)", "track": "Cap (°)", "squawk": "Squawk",
        }
        for col in columns:
            self.aircraft_tree.heading(col, text=headings[col])
            self.aircraft_tree.column(col, width=95, minwidth=60, anchor="center")
        self.aircraft_tree.pack(fill="both", expand=True, padx=1, pady=1)
        self.aircraft_tree.bind("<<TreeviewSelect>>", self._on_aircraft_tree_select)

        detail_outer = theme.card(wrapper)
        detail_outer.pack(fill="x", padx=12, pady=(0, 6))
        detail_inner = detail_outer.inner
        detail_inner.configure(padding=8)

        self.aircraft_photo_label = tk.Label(
            detail_inner, bg=theme.BG_CARD_ALT, fg=theme.MUTED, text="—",
            font=theme.FONT_SUBTITLE, width=20, height=6,
        )
        self.aircraft_photo_label.pack(side="left", padx=(0, 14))

        text_frame = tk.Frame(detail_inner, bg=theme.BG_CARD)
        text_frame.pack(side="left", fill="both", expand=True)
        self.aircraft_detail_title = tk.Label(
            text_frame, text="Cliquez un avion (carte ou tableau) pour voir son détail.",
            bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY_BOLD, anchor="w", justify="left",
        )
        self.aircraft_detail_title.pack(anchor="w")

        # Grille de champs façon tar1090 (spatial / signal), remplie au
        # clic sur un avion (carte ou tableau).
        fields_frame = tk.Frame(text_frame, bg=theme.BG_CARD)
        fields_frame.pack(anchor="w", fill="x", pady=(6, 0))
        detail_field_titles = [
            ("gs", "Vitesse sol"), ("position", "Position"),
            ("alt_baro", "Altitude baro."), ("distance", "Distance station"),
            ("alt_geom", "Altitude géo."), ("source", "Source"),
            ("vrate", "Vitesse verticale"), ("rssi", "Signal (RSSI)"),
            ("track", "Cap"), ("messages", "Messages"),
            ("squawk", "Squawk"), ("seen", "Vu / position il y a"),
        ]
        self.aircraft_detail_value_labels = {}
        for i, (key, title) in enumerate(detail_field_titles):
            r, c = divmod(i, 4)
            cell = tk.Frame(fields_frame, bg=theme.BG_CARD)
            cell.grid(row=r, column=c, sticky="w", padx=(0, 16), pady=1)
            tk.Label(
                cell, text=title, bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", 8),
            ).pack(anchor="w")
            value_label = tk.Label(cell, text="—", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY)
            value_label.pack(anchor="w")
            self.aircraft_detail_value_labels[key] = value_label

        self.aircraft_detail_enriched = tk.Label(
            text_frame, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY,
            anchor="w", justify="left", wraplength=400,
        )
        self.aircraft_detail_enriched.pack(anchor="w", pady=(6, 0))
        self.aircraft_detail_credit = tk.Label(
            text_frame, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", 8), anchor="w",
        )
        self.aircraft_detail_credit.pack(anchor="w", pady=(4, 0))

        map_outer = theme.card(outer_wrapper)
        map_outer.pack(side="left", fill="both", expand=True, pady=(8, 12), padx=(0, 12))
        self.aircraft_map = TkinterMapView(map_outer.inner, corner_radius=0)
        self.aircraft_map.pack(fill="both", expand=True, padx=1, pady=1)
        lat, lon = DEFAULT_COORDS
        self.aircraft_map.set_position(lat, lon)
        self.aircraft_map.set_zoom(8)

        self._aircraft_icon_refs = []
        self._aircraft_trails = {}
        self._aircraft_info_cache = {}
        self._selected_aircraft_hex = None
        self._current_photo_image = None
        self._last_aircraft_full = []
        self._last_aircraft_by_hex = {}

    # -- Sélection d'un avion (carte ou tableau) et infos enrichies --

    def _on_aircraft_tree_select(self, _event):
        selection = self.aircraft_tree.selection()
        if not selection:
            return
        ac = self._last_aircraft_by_hex.get(selection[0])
        if ac:
            self._show_aircraft_detail_data(ac)

    def _on_aircraft_marker_click(self, marker):
        self._show_aircraft_detail_data(marker.data)

    def _show_aircraft_detail_data(self, ac):
        hx = ac.get("hex")
        flight = (ac.get("flight") or "").strip() or hx or "—"
        self._selected_aircraft_hex = hx

        self.aircraft_detail_title.config(text=f"{flight}  ·  hex {(hx or '—').upper()}")

        gs = ac.get("gs")
        lat, lon = ac.get("lat"), ac.get("lon")
        station_lat, station_lon = DEFAULT_COORDS
        distance_nm = _haversine_nm(station_lat, station_lon, lat, lon) if lat is not None and lon is not None else None
        vert_rate = ac.get("baro_rate")
        if vert_rate is None:
            vert_rate = ac.get("geom_rate")

        values = {
            "gs": f"{_fmt(gs)} kt ({_fmt(_kt_to_kmh(gs))} km/h)",
            "position": f"{lat:.3f}°, {lon:.3f}°" if lat is not None and lon is not None else "—",
            "alt_baro": f"{_fmt(ac.get('alt_baro'))} ft",
            "distance": f"{distance_nm:.1f} nm" if distance_nm is not None else "—",
            "alt_geom": f"{_fmt(ac.get('alt_geom'))} ft",
            "source": _source_label(ac.get("type")),
            "vrate": f"{_fmt(vert_rate)} ft/min",
            "rssi": f"{_fmt(ac.get('rssi'))} dBFS",
            "track": f"{_fmt(ac.get('track'))}°",
            "messages": _fmt(ac.get("messages")),
            "squawk": _fmt(ac.get("squawk")),
            "seen": f"{_fmt(ac.get('seen'))} s / {_fmt(ac.get('seen_pos'))} s",
        }
        for key, label in self.aircraft_detail_value_labels.items():
            label.config(text=values.get(key, "—"))

        cached = self._aircraft_info_cache.get(hx) if hx else None
        if cached == "loading":
            self.aircraft_detail_enriched.config(text="Recherche des informations avion...")
        elif cached:
            self._apply_enriched_info(hx, cached)
        else:
            self.aircraft_detail_enriched.config(text="Recherche des informations avion...")
            self.aircraft_detail_credit.config(text="")
            self._current_photo_image = None
            self.aircraft_photo_label.config(image="", text="—")
            if hx:
                self._aircraft_info_cache[hx] = "loading"
                threading.Thread(target=self._fetch_aircraft_info, args=(hx,), daemon=True).start()

    def _fetch_aircraft_info(self, hx):
        """Interroge hexdb.io (type/compagnie/immatriculation) et
        planespotters.net (photo) — services publics tiers, gratuits et
        sans clé, utilisés notamment par tar1090 pour ces mêmes données."""
        info = {"type": None, "operator": None, "registration": None,
                "photo_url": None, "photographer": None, "_photo_image": None}

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
                payload = resp.json()
                photos = payload.get("photos") or []
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
                img.thumbnail((160, 110))
                info["_photo_image"] = ImageTk.PhotoImage(img)
            except Exception:
                info["_photo_image"] = None

        self._aircraft_info_cache[hx] = info
        self.after(0, self._apply_enriched_info, hx, info)

    def _apply_enriched_info(self, hx, info):
        if hx != self._selected_aircraft_hex:
            return  # une autre sélection a eu lieu entre-temps

        parts = []
        if info.get("operator"):
            parts.append(info["operator"])
        if info.get("type"):
            parts.append(info["type"])
        if info.get("registration"):
            parts.append(f"immat. {info['registration']}")
        self.aircraft_detail_enriched.config(
            text=" · ".join(parts) if parts else "Informations avion indisponibles (hexdb.io).",
        )

        photo_image = info.get("_photo_image")
        if photo_image is not None:
            self._current_photo_image = photo_image
            self.aircraft_photo_label.config(image=photo_image, text="")
            credit = info.get("photographer")
            self.aircraft_detail_credit.config(
                text=f"Photo © {credit} — planespotters.net" if credit else "Photo : planespotters.net",
            )
        else:
            self._current_photo_image = None
            self.aircraft_photo_label.config(image="", text="Pas de photo")
            self.aircraft_detail_credit.config(text="")

    # -- Rendu --

    def _render_aircraft(self, data, error):
        if error:
            self.aircraft_count_label.config(text="Avions détectés : indisponible")
            self._last_aircraft_full = []
            self._last_aircraft_by_hex = {}
            self._repaint_aircraft()
            if "aircraft" in self.tile_chips:
                self.tile_chips["aircraft"].config(text="—")
            self.chip_aircraft.config(text="Avions : —")
            return

        aircraft = data.get("aircraft", [])
        self._last_aircraft_full = aircraft
        self._last_aircraft_by_hex = {ac["hex"]: ac for ac in aircraft if ac.get("hex")}

        new_trails = {}
        for ac in aircraft:
            hx, lat, lon = ac.get("hex"), ac.get("lat"), ac.get("lon")
            if hx and lat is not None and lon is not None:
                trail = self._aircraft_trails.get(hx) or deque(maxlen=15)
                trail.append((lat, lon))
                new_trails[hx] = trail
        self._aircraft_trails = new_trails

        self.aircraft_count_label.config(text=f"Avions détectés : {len(aircraft)}")
        if "aircraft" in self.tile_chips:
            self.tile_chips["aircraft"].config(text=f"{len(aircraft)} avion(s)")
        self.chip_aircraft.config(text=f"Avions : {len(aircraft)}")

        self._repaint_aircraft()

    def _repaint_aircraft(self):
        query = self.aircraft_search_var.get().strip().lower()
        if query:
            aircraft = [
                ac for ac in self._last_aircraft_full
                if query in (ac.get("flight") or "").strip().lower() or query in (ac.get("hex") or "").lower()
            ]
        else:
            aircraft = self._last_aircraft_full

        for item in self.aircraft_tree.get_children():
            self.aircraft_tree.delete(item)
        self.aircraft_map.delete_all_marker()
        self.aircraft_map.delete_all_path()
        self._aircraft_icon_refs = []

        for ac in aircraft:
            flight = (ac.get("flight") or "").strip() or "—"
            hx = ac.get("hex")
            row_kwargs = {"iid": hx} if hx else {}
            try:
                self.aircraft_tree.insert(
                    "", "end",
                    values=(flight, hx or "—", ac.get("alt_baro", "—"),
                            ac.get("gs", "—"), ac.get("track", "—"), ac.get("squawk", "—")),
                    **row_kwargs,
                )
            except tk.TclError:
                self.aircraft_tree.insert(
                    "", "end",
                    values=(flight, hx or "—", ac.get("alt_baro", "—"),
                            ac.get("gs", "—"), ac.get("track", "—"), ac.get("squawk", "—")),
                )

            lat, lon = ac.get("lat"), ac.get("lon")
            if lat is not None and lon is not None:
                icon = _plane_icon(ac.get("alt_baro"), ac.get("track"))
                self._aircraft_icon_refs.append(icon)
                self.aircraft_map.set_marker(
                    lat, lon, text=flight, icon=icon, icon_anchor="center",
                    data=ac, command=self._on_aircraft_marker_click,
                )
                trail = self._aircraft_trails.get(hx)
                if trail and len(trail) >= 2:
                    self.aircraft_map.set_path(list(trail), color=_alt_color(ac.get("alt_baro")), width=2)

    # ----------------------------------------------------------- APRS --

    def _build_aprs_tab(self, frame):
        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        status_outer = theme.card(wrapper)
        status_outer.pack(fill="x", padx=12, pady=12)
        status_inner = status_outer.inner
        status_inner.configure(padding=16)

        tk.Label(
            status_inner, text="APRS (Direwolf)", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION,
        ).pack(side="left")
        self.aprs_badge = theme.pill(status_inner, "INCONNU", theme.MUTED)
        self.aprs_badge.pack(side="left", padx=(12, 0))
        ttk.Button(
            status_inner, text="Redémarrer Direwolf", style="Flat.TButton",
            command=lambda: self._restart_service("direwolf"),
        ).pack(side="right")
        ttk.Button(
            status_inner, text="Carte aprs.fi (F4JCF-10)", style="Flat.TButton",
            command=lambda: self._open_url("https://aprs.fi/#!call=a%2FF4JCF-10&timerange=3600&tail=3600"),
        ).pack(side="right", padx=(0, 8))

        self.aprs_count_label = tk.Label(
            wrapper, text="Stations entendues : —", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION,
        )
        self.aprs_count_label.pack(anchor="w", padx=12, pady=(4, 6))

        table_outer = theme.card(wrapper)
        table_outer.pack(fill="both", padx=12, pady=(0, 8))
        columns = ("callsign", "heard", "position", "symbol", "comment")
        self.aprs_tree = ttk.Treeview(table_outer.inner, columns=columns, show="headings", height=6)
        headings = {
            "callsign": "Indicatif", "heard": "Dernière réception", "position": "Position",
            "symbol": "Symbole", "comment": "Commentaire",
        }
        widths = {"callsign": 110, "heard": 150, "position": 220, "symbol": 70, "comment": 260}
        for col in columns:
            self.aprs_tree.heading(col, text=headings[col])
            self.aprs_tree.column(col, width=widths[col], anchor="center" if col != "comment" else "w")
        self.aprs_tree.pack(fill="both", expand=True, padx=1, pady=1)

        map_outer = theme.card(wrapper)
        map_outer.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.aprs_map = TkinterMapView(map_outer.inner, corner_radius=0)
        self.aprs_map.pack(fill="both", expand=True, padx=1, pady=1)
        lat, lon = DEFAULT_COORDS
        self.aprs_map.set_position(lat, lon)
        self.aprs_map.set_zoom(9)

    def _render_aprs_stations(self, data, error):
        if error:
            self.aprs_count_label.config(
                text="Stations entendues : indisponible — aprs-api.service est-il déployé sur le Pi ?",
            )
            for item in self.aprs_tree.get_children():
                self.aprs_tree.delete(item)
            self.aprs_map.delete_all_marker()
            return

        self.aprs_count_label.config(text=f"Stations entendues : {len(data)}")
        for item in self.aprs_tree.get_children():
            self.aprs_tree.delete(item)
        self.aprs_map.delete_all_marker()

        for st in data:
            last_heard = st.get("last_heard")
            heard_txt = time.strftime("%H:%M:%S", time.localtime(last_heard)) if last_heard else "—"
            lat, lon = st.get("lat"), st.get("lon")
            if lat is not None and lon is not None:
                dist = st.get("distance_km")
                compass = st.get("bearing_compass", "")
                position_txt = f"{lat:.4f}, {lon:.4f}"
                if dist is not None:
                    position_txt += f"  ({dist} km {compass})"
            else:
                position_txt = "—"
            self.aprs_tree.insert(
                "", "end",
                values=(
                    st.get("callsign", "—"), heard_txt, position_txt,
                    st.get("symbol", "—"), st.get("comment", ""),
                ),
            )
            if lat is not None and lon is not None:
                self.aprs_map.set_marker(lat, lon, text=st.get("callsign", ""))

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
        self.ais_map = TkinterMapView(map_outer.inner, corner_radius=0)
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
                txt = f"Navires : AIS en pause — la clé SDR est sur {label}. Ouvrez cette page pour la récupérer."
            else:
                txt = "Navires : indisponible — AIS-catcher est-il démarré sur le Pi ?"
            self.ais_count_label.config(text=txt)
            if "ais" in self.tile_chips:
                self.tile_chips["ais"].config(text="—")
            self._repaint_ais(keep_label=True)
            return

        ships = data.get("ships", []) if isinstance(data, dict) else []
        self._ais_ships = ships
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
        for mode in ("adsb", "ais", "aprs", "openwebrx"):
            b = tk.Button(
                btns, text=SDR_MODE_LABELS[mode], font=theme.FONT_BODY_BOLD, relief="flat", bd=0,
                bg=theme.BG_CARD_ALT, fg=theme.TEXT, activebackground=theme.ACCENT, activeforeground="#1a1200",
                padx=16, pady=6, cursor="hand2", command=lambda m=mode: self._switch_sdr(m),
            )
            b.pack(side="left", padx=4)
            self.sdr_mode_buttons[mode] = b

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

        mode_names = [SDR_MODE_LABELS[m] for m in ("adsb", "ais", "aprs", "openwebrx")]
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
        self.sdr_mode_value.config(
            text=SDR_MODE_LABELS.get(mode, mode or "—"), fg=theme.ACCENT if active else theme.ORANGE,
        )
        sched = data.get("schedule") or {}
        parts = ["service en marche" if active else "service arrêté ou en échec (clé débranchée ?)"]
        names = {"manual": "manuel", "rotation": "rotation", "planning": "planning horaire"}
        parts.append(f"partage : {names.get(sched.get('type'), '—')}")
        hold = data.get("hold_until")
        if hold and hold > time.time():
            parts.append(f"pause jusqu'à {time.strftime('%H:%M', time.localtime(hold))}")
        nxt = data.get("next_change")
        if nxt and sched.get("type") != "manual":
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
        if "sdr" in self.tile_chips:
            self.tile_chips["sdr"].config(text=f"Clé sur {SDR_MODE_LABELS.get(mode, mode)} · {names.get(sched.get('type'), '—')}")

    # ---------------------------------------------------- Vignettes accueil --

    def _refresh_all_tiles(self):
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
        self.health_sys_label.pack(anchor="w", padx=14, pady=(4, 0))

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
        self.destroy()

    def _on_callback_exception(self, exc_type, exc_value, exc_tb):
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _log_crash(tb_text, context="callback")


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
