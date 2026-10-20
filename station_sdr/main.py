"""STATION-SDR - Tableau de bord Windows pour le Raspberry Pi F4JCF-SDR-STATION.

Interface Tkinter : bandeau d'en-tête, puces de statut global, écran
d'accueil en vignettes cliquables (météo, trafic aérien, APRS, Winlink,
récepteurs SDR, services) ouvrant chacune un module dédié, rafraîchis
périodiquement en arrière-plan, plus mise à jour automatique au démarrage.
"""

import io
import math
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
    ("sdr", "Récepteurs SDR", "Allocation des tuners"),
    ("svxlink", "SvxLink", "Relais vocal / Echolink"),
    ("openwebrx", "OpenWebRX", "Réception SDR (ouvre le navigateur)"),
    ("services", "Services système", "Vue d'ensemble des services"),
]

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
        self._build_info_tab(
            self.pages["ais"], "AIS", "ais",
            "Suivi du trafic maritime (navires équipés d'un transpondeur AIS) par\n"
            "récepteur SDR, décodé par ex. par rtl_ais ou AIS-catcher sur le Pi.\n\n"
            "Le statut ci-dessous reflète le service tel que rapporté par\n"
            "station_api.py. Pour afficher ici la liste des navires détectés\n"
            "(position, cap, MMSI...), il faudrait une petite API dédiée, comme\n"
            "aprs_api.py pour l'APRS.",
        )
        self._build_info_tab(
            self.pages["svxlink"], "SvxLink", "svxlink",
            "Relais vocal radioamateur (répéteur logiciel, passerelle Echolink/\n"
            "DMR/RoIP selon configuration) tournant sur le Pi.\n\n"
            "Le statut ci-dessous reflète le service tel que rapporté par\n"
            "station_api.py. Pour afficher ici l'activité en direct (dernier\n"
            "indicatif entendu, nœuds Echolink connectés...), il faudrait exposer\n"
            "ces informations via une petite API dédiée.",
        )

        self.show_home()

    def _build_home_tiles(self):
        grid = tk.Frame(self.home_frame, bg=theme.BG)
        grid.pack(fill="both", expand=True, padx=4, pady=4)

        self.tile_chips = {}
        columns = 3
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
            self.tile_chips[key] = chip_label

            for widget in (outer, inner, header_row, icon_label, title_label, subtitle_label, chip_label):
                widget.bind("<Button-1>", lambda _e, k=key: self.show_page(k))
                try:
                    widget.configure(cursor="hand2")
                except tk.TclError:
                    pass

    def show_home(self):
        self._show_view(None)

    def show_page(self, key):
        if key in EXTERNAL_TILES:
            url = EXTERNAL_TILES[key](self.config_data)
            try:
                webbrowser.open(url)
            except Exception as exc:
                messagebox.showerror("Ouverture", f"Impossible d'ouvrir {url} :\n{exc}")
            return
        self._show_view(key)

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
        self._fetch(lambda: api_client.get_sdr_status(host, cfg["port_sdr"]), self._render_sdr)
        self._fetch(lambda: api_client.get_aprs_stations(host, cfg["port_aprs"]), self._render_aprs_stations)
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
        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        top_row = tk.Frame(wrapper, bg=theme.BG)
        top_row.pack(fill="x", padx=12, pady=(8, 6))
        self.aircraft_count_label = tk.Label(
            top_row, text="Avions détectés : —", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION,
        )
        self.aircraft_count_label.pack(side="left")

        search_box = tk.Frame(top_row, bg=theme.BG)
        search_box.pack(side="right")
        tk.Label(
            search_box, text="Rechercher (indicatif / hex) :", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE,
        ).pack(side="left", padx=(0, 6))
        self.aircraft_search_var = tk.StringVar()
        self.aircraft_search_var.trace_add("write", lambda *_a: self._repaint_aircraft())
        ttk.Entry(search_box, textvariable=self.aircraft_search_var, width=16).pack(side="left")

        table_outer = theme.card(wrapper)
        table_outer.pack(fill="x", padx=12, pady=(0, 6))
        columns = ("flight", "hex", "alt", "gs", "track", "squawk")
        self.aircraft_tree = ttk.Treeview(table_outer.inner, columns=columns, show="headings", height=3)
        headings = {
            "flight": "Indicatif", "hex": "Hex", "alt": "Altitude (ft)",
            "gs": "Vitesse (kt)", "track": "Cap (°)", "squawk": "Squawk",
        }
        for col in columns:
            self.aircraft_tree.heading(col, text=headings[col])
            self.aircraft_tree.column(col, width=130, anchor="center")
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
            anchor="w", justify="left", wraplength=560,
        )
        self.aircraft_detail_enriched.pack(anchor="w", pady=(6, 0))
        self.aircraft_detail_credit = tk.Label(
            text_frame, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", 8), anchor="w",
        )
        self.aircraft_detail_credit.pack(anchor="w", pady=(4, 0))

        map_outer = theme.card(wrapper)
        map_outer.pack(fill="both", expand=True, padx=12, pady=(0, 12))
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

    def _render_sdr(self, data, error):
        frame = self.pages["sdr"]
        if error:
            self._render_error(frame, error)
            return
        self._clear(frame)

        in_use = sum(1 for v in data.values() if v)
        self.chip_sdr.config(text=f"SDR : {in_use}/{len(data)} occupé(s)")
        if "sdr" in self.tile_chips:
            self.tile_chips["sdr"].config(text=f"{in_use}/{len(data)} occupé(s)")

        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        grid = tk.Frame(wrapper, bg=theme.BG)
        grid.pack(fill="both", expand=True, padx=8, pady=8)

        names = {"adsb": "ADS-B", "ais": "AIS", "aprs": "APRS", "sdr": "Récepteur SDR"}
        for i, (key, used) in enumerate(sorted(data.items())):
            outer = theme.card(grid)
            outer.grid(row=i // 2, column=i % 2, sticky="nsew", padx=10, pady=10)
            grid.grid_columnconfigure(i % 2, weight=1)
            inner = outer.inner
            inner.configure(padding=16)
            tk.Label(
                inner, text=names.get(key, key), bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION,
            ).pack(anchor="w")
            badge = theme.pill(inner, "OCCUPÉ" if used else "LIBRE", theme.ORANGE if used else theme.GREEN)
            badge.pack(anchor="w", pady=(10, 0))

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
