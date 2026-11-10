"""Vignette « Météo du moment » de l'écran d'accueil, alimentée par
Open-Meteo (https://open-meteo.com) : API publique et gratuite, sans clé.

Ce sont des valeurs de modèle (prévision « du moment », rafraîchies toutes
les 15 minutes chez Open-Meteo) calculées pour la position de la station
active (profil de station), pas des mesures d'un capteur. Les mesures
réelles restent sur la page Météo (METAR, capteur 433 MHz).

La requête part du PC qui fait tourner STATION-SDR (connexion Internet
nécessaire), pas du Raspberry Pi."""

import math
import threading
import time
import tkinter as tk
from datetime import datetime

import requests

from . import theme
from .config import save_config

OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"
OM_REFRESH_S = 600  # Open-Meteo ne recalcule « current » que toutes les 15 min

# Codes météo OMM (WMO) renvoyés par Open-Meteo -> libellé court.
WMO_LABELS = {
    0: "Ciel clair", 1: "Peu nuageux", 2: "Partiellement nuageux", 3: "Couvert",
    45: "Brouillard", 48: "Brouillard givrant",
    51: "Bruine légère", 53: "Bruine", 55: "Bruine forte",
    56: "Bruine verglaçante", 57: "Bruine verglaçante forte",
    61: "Pluie faible", 63: "Pluie", 65: "Pluie forte",
    66: "Pluie verglaçante", 67: "Pluie verglaçante forte",
    71: "Neige faible", 73: "Neige", 75: "Neige forte", 77: "Grains de neige",
    80: "Averses faibles", 81: "Averses", 82: "Averses violentes",
    85: "Averses de neige", 86: "Averses de neige fortes",
    95: "Orage", 96: "Orage avec grêle", 99: "Orage avec forte grêle",
}

DIRS16 = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
          "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO")


def _num(v, fmt="{:.1f}"):
    if v is None:
        return "—"
    return fmt.format(v).replace(".", ",")


def _dir16(deg):
    if deg is None:
        return "—"
    return DIRS16[int((deg % 360) / 22.5 + 0.5) % 16]


def _uv_level(uv):
    """Niveau OMS de l'indice UV -> (libellé, couleur)."""
    if uv is None:
        return "", theme.MUTED
    if uv < 3:
        return "faible", theme.GREEN
    if uv < 6:
        return "modéré", "#e3c341"
    if uv < 8:
        return "élevé", theme.ORANGE
    if uv < 11:
        return "très élevé", theme.RED
    return "extrême", "#b66dff"


def fetch_openmeteo(lat, lon, timeout=8):
    """Une seule requête : conditions du moment + heures (24 h passées et à
    venir, pour la tendance de pression, l'UV et le rayonnement) + cumuls du
    jour. Lève requests.RequestException / ValueError en cas d'échec."""
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": ",".join((
            "temperature_2m", "relative_humidity_2m", "apparent_temperature", "dew_point_2m",
            "precipitation", "weather_code", "pressure_msl", "wind_speed_10m",
            "wind_direction_10m", "wind_gusts_10m", "is_day",
        )),
        "hourly": "pressure_msl,uv_index,shortwave_radiation,temperature_2m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,uv_index_max",
        "timezone": "auto",
        "past_days": 1,
        "forecast_days": 2,
        "wind_speed_unit": "kmh",
    }
    resp = requests.get(OPENMETEO_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise ValueError(data.get("reason") or "réponse Open-Meteo invalide")
    return data


class _Compass(tk.Canvas):
    """Petite rose des vents : flèche dans le sens où souffle le vent."""

    def __init__(self, parent, size=58):
        self.size = theme.px(size)
        super().__init__(parent, width=self.size, height=self.size, bg=theme.BG_CARD, highlightthickness=0)
        self.deg = None
        self._draw()

    def set(self, deg):
        self.deg = deg
        self._draw()

    def _draw(self):
        self.delete("all")
        s = self.size
        c, r = s / 2, s / 2 - 4
        self.create_oval(c - r, c - r, c + r, c + r, outline=theme.BORDER, width=2)
        for i in range(16):
            a = math.radians(i * 22.5)
            r2 = r - (6 if i % 4 == 0 else 3)
            self.create_line(c + r * math.sin(a), c - r * math.cos(a),
                             c + r2 * math.sin(a), c - r2 * math.cos(a), fill=theme.MUTED)
        self.create_text(c, c - r + 10, text="N", fill=theme.MUTED, font=("Segoe UI", theme.fs(7), "bold"))
        if self.deg is None:
            return
        # Direction météo = d'où vient le vent ; la flèche montre où il va.
        a = math.radians((self.deg + 180) % 360)
        tip = (c + (r - 8) * math.sin(a), c - (r - 8) * math.cos(a))
        tail = (c - (r - 10) * math.sin(a), c + (r - 10) * math.cos(a))
        self.create_line(*tail, *tip, fill=theme.ACCENT, width=3, arrow="last",
                         arrowshape=(9, 11, 4), capstyle="round")


class _Ring(tk.Canvas):
    """Anneau de pourcentage (humidité)."""

    def __init__(self, parent, size=58):
        self.size = theme.px(size)
        super().__init__(parent, width=self.size, height=self.size, bg=theme.BG_CARD, highlightthickness=0)
        self.value = None
        self._draw()

    def set(self, v):
        self.value = v
        self._draw()

    def _draw(self):
        self.delete("all")
        s, w = self.size, theme.px(7)
        box = (w, w, s - w, s - w)
        self.create_oval(*box, outline=theme.BG_CARD_ALT, width=w)
        if self.value is None:
            return
        ext = -max(0.0, min(100.0, self.value)) / 100 * 359.9
        self.create_arc(*box, start=90, extent=ext, style="arc", outline=theme.BLUE, width=w)


class _Spark(tk.Canvas):
    """Mini-courbe (pression sur 24 h), repère « maintenant » à droite."""

    def __init__(self, parent, width=150, height=42):
        super().__init__(parent, width=theme.px(width), height=theme.px(height),
                         bg=theme.BG_CARD, highlightthickness=0)
        self.values = []
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, values):
        self.values = [v for v in values if v is not None]
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = max(self.winfo_width(), 40), max(self.winfo_height(), 20)
        if len(self.values) < 2:
            return
        lo, hi = min(self.values), max(self.values)
        span = max(hi - lo, 1.0)
        pad = 4
        pts = []
        n = len(self.values) - 1
        for i, v in enumerate(self.values):
            pts += [pad + i / n * (w - 2 * pad), h - pad - (v - lo) / span * (h - 2 * pad)]
        self.create_line(*pts, fill=theme.BLUE, width=2, smooth=True)
        self.create_oval(pts[-2] - 3, pts[-1] - 3, pts[-2] + 3, pts[-1] + 3, fill=theme.TEXT, outline="")


class OpenMeteoMixin:
    # ------------------------------------------------------------ Vignette --

    def _build_openmeteo_band(self, parent):
        """Bandeau « Météo du moment » sous les vu-mètres de l'accueil. Un clic
        ouvre la page Météo."""
        self._om_data = None
        self._om_last_fetch = 0.0
        self._om_coords = None
        self._om_busy = False

        outer = theme.card(parent)
        self._om_outer = outer
        if self.config_data.get("home_openmeteo", True):
            outer.pack(fill="x", padx=14, pady=(0, 6))
        row = outer.inner
        row.configure(padding=(12, 8))
        bg = theme.BG_CARD

        def cell(col, title, weight=1):
            f = tk.Frame(row, bg=bg)
            f.grid(row=0, column=col, sticky="nsew", padx=(0, 14))
            row.grid_columnconfigure(col, weight=weight)
            tk.Label(f, text=title, bg=bg, fg=theme.MUTED,
                     font=("Segoe UI", theme.fs(9), "bold")).pack(anchor="w")
            return f

        big = ("Segoe UI", theme.fs(22), "bold")
        val = ("Segoe UI", theme.fs(14), "bold")
        sub = ("Segoe UI", theme.fs(9))
        self._om_w = w = {}

        # 0 — Température + ciel
        f = cell(0, "MÉTÉO DU MOMENT", weight=2)
        w["temp"] = tk.Label(f, text="—", bg=bg, fg=theme.TEXT, font=big)
        w["temp"].pack(anchor="w")
        w["sky"] = tk.Label(f, text="", bg=bg, fg=theme.TEXT, font=theme.FONT_BODY_BOLD)
        w["sky"].pack(anchor="w")
        w["feel"] = tk.Label(f, text="", bg=bg, fg=theme.MUTED, font=sub)
        w["feel"].pack(anchor="w")

        # 1 — Humidité (anneau)
        f = cell(1, "HUMIDITÉ")
        line = tk.Frame(f, bg=bg)
        line.pack(anchor="w", pady=(2, 0))
        w["ring"] = _Ring(line)
        w["ring"].pack(side="left")
        col = tk.Frame(line, bg=bg)
        col.pack(side="left", padx=(8, 0))
        w["hum"] = tk.Label(col, text="—", bg=bg, fg=theme.TEXT, font=val)
        w["hum"].pack(anchor="w")
        w["dew"] = tk.Label(col, text="", bg=bg, fg=theme.MUTED, font=sub)
        w["dew"].pack(anchor="w")

        # 2 — Vent (rose)
        f = cell(2, "VENT")
        line = tk.Frame(f, bg=bg)
        line.pack(anchor="w", pady=(2, 0))
        w["compass"] = _Compass(line)
        w["compass"].pack(side="left")
        col = tk.Frame(line, bg=bg)
        col.pack(side="left", padx=(8, 0))
        w["wind"] = tk.Label(col, text="—", bg=bg, fg=theme.TEXT, font=val)
        w["wind"].pack(anchor="w")
        w["gust"] = tk.Label(col, text="", bg=bg, fg=theme.MUTED, font=sub, justify="left")
        w["gust"].pack(anchor="w")

        # 3 — Précipitations
        f = cell(3, "PRÉCIPITATIONS")
        w["rain_day"] = tk.Label(f, text="—", bg=bg, fg=theme.TEXT, font=val)
        w["rain_day"].pack(anchor="w", pady=(6, 0))
        w["rain_now"] = tk.Label(f, text="", bg=bg, fg=theme.MUTED, font=sub)
        w["rain_now"].pack(anchor="w")

        # 4 — Pression + tendance 24 h
        f = cell(4, "PRESSION", weight=2)
        line = tk.Frame(f, bg=bg)
        line.pack(anchor="w", fill="x")
        w["press"] = tk.Label(line, text="—", bg=bg, fg=theme.TEXT, font=val)
        w["press"].pack(side="left")
        w["trend"] = tk.Label(line, text="", bg=bg, fg=theme.MUTED, font=theme.FONT_BODY_BOLD)
        w["trend"].pack(side="left", padx=(6, 0))
        w["spark"] = _Spark(f)
        w["spark"].pack(anchor="w", fill="x")
        w["press_sub"] = tk.Label(f, text="", bg=bg, fg=theme.MUTED, font=sub)
        w["press_sub"].pack(anchor="w")

        # 5 — UV / rayonnement
        f = cell(5, "UV · SOLEIL")
        line = tk.Frame(f, bg=bg)
        line.pack(anchor="w", pady=(6, 0))
        w["uv"] = tk.Label(line, text="—", bg=bg, fg=theme.TEXT, font=val)
        w["uv"].pack(side="left")
        w["uv_lvl"] = tk.Label(line, text="", bg=bg, fg=theme.MUTED, font=theme.FONT_BODY_BOLD)
        w["uv_lvl"].pack(side="left", padx=(6, 0))
        w["rad"] = tk.Label(f, text="", bg=bg, fg=theme.MUTED, font=sub)
        w["rad"].pack(anchor="w")

        # 6 — Min / max du jour + source
        f = cell(6, "AUJOURD'HUI")
        w["minmax"] = tk.Label(f, text="—", bg=bg, fg=theme.TEXT, font=val)
        w["minmax"].pack(anchor="w", pady=(6, 0))
        w["src"] = tk.Label(f, text="Open-Meteo · modèle", bg=bg, fg=theme.MUTED, font=sub, justify="left")
        w["src"].pack(anchor="w")

        def bind_all(widget):
            widget.bind("<Button-1>", lambda _e: self.show_page("weather"))
            try:
                widget.configure(cursor="hand2")
            except tk.TclError:
                pass
            for ch in widget.winfo_children():
                bind_all(ch)

        bind_all(outer)

    def _toggle_openmeteo_band(self):
        show = bool(self._om_show_var.get())
        self.config_data["home_openmeteo"] = show
        save_config(self.config_data)
        if not hasattr(self, "_om_outer"):
            return
        if show:
            # Juste sous le bandeau des vu-mètres, au-dessus des vignettes.
            self._om_outer.pack(fill="x", padx=14, pady=(0, 6), after=self._om_outer.master.winfo_children()[0])
            self._om_last_fetch = 0.0
            self._om_maybe_fetch()
        else:
            self._om_outer.pack_forget()

    # ------------------------------------------------------------ Données --

    def _om_maybe_fetch(self):
        """Appelé à chaque cycle de rafraîchissement (thread de fond) : ne
        relance une requête que toutes les 10 min, ou tout de suite si la
        station active (donc la position) a changé."""
        if not hasattr(self, "_om_w") or not self.config_data.get("home_openmeteo", True):
            return
        try:
            coords = self._station_coords()
        except Exception:
            return
        if self._om_busy:
            return
        if coords == self._om_coords and time.time() - self._om_last_fetch < OM_REFRESH_S:
            return
        self._om_busy = True

        def worker():
            try:
                data = fetch_openmeteo(*coords)
                err = None
            except requests.RequestException as exc:
                data, err = None, f"Open-Meteo injoignable ({exc.__class__.__name__})"
            except Exception as exc:
                data, err = None, f"réponse illisible ({exc})"
            self.after(0, self._om_done, coords, data, err)

        threading.Thread(target=worker, daemon=True).start()

    def _om_done(self, coords, data, err):
        self._om_busy = False
        self._om_last_fetch = time.time()
        if err:
            # On garde les dernières valeurs affichées, on signale seulement.
            self._om_w["src"].config(text=f"Open-Meteo · {err}", fg=theme.RED)
            if self._om_data is None:
                self._om_coords = None  # réessaie au prochain cycle
            return
        self._om_coords = coords
        self._om_data = data
        try:
            self._om_render(data)
        except Exception as exc:  # garde-fou : la vignette ne doit pas planter l'appli
            self._om_w["src"].config(text=f"Open-Meteo · affichage impossible ({exc})", fg=theme.RED)

    def _om_render(self, data):
        w = self._om_w
        cur = data.get("current") or {}
        hourly = data.get("hourly") or {}
        daily = data.get("daily") or {}

        t = cur.get("temperature_2m")
        w["temp"].config(text=f"{_num(t)} °C")
        code = cur.get("weather_code")
        w["sky"].config(text=WMO_LABELS.get(code, "") + ("" if cur.get("is_day", 1) else " · nuit"))
        w["feel"].config(text=f"ressenti {_num(cur.get('apparent_temperature'))} °C")

        h = cur.get("relative_humidity_2m")
        w["ring"].set(h)
        w["hum"].config(text=f"{_num(h, '{:.0f}')} %")
        w["dew"].config(text=f"rosée {_num(cur.get('dew_point_2m'))} °C")

        ws, wg, wd = cur.get("wind_speed_10m"), cur.get("wind_gusts_10m"), cur.get("wind_direction_10m")
        w["compass"].set(wd if ws else None)
        w["wind"].config(text=f"{_num(ws, '{:.0f}')} km/h")
        w["gust"].config(text=f"rafales {_num(wg, '{:.0f}')} km/h\n{_dir16(wd)}"
                              + (f" · {wd:.0f}°" if wd is not None else ""))

        # Index de l'heure en cours dans la série horaire (heure locale, timezone=auto).
        times = hourly.get("time") or []
        now_key = (cur.get("time") or datetime.now().strftime("%Y-%m-%dT%H:%M"))[:13]
        idx = next((i for i, s in enumerate(times) if s[:13] == now_key), None)

        def hv(name, i):
            arr = hourly.get(name) or []
            return arr[i] if i is not None and 0 <= i < len(arr) else None

        today = (cur.get("time") or "")[:10]
        days = daily.get("time") or []
        di = days.index(today) if today in days else None

        def dv(name):
            arr = daily.get(name) or []
            return arr[di] if di is not None and di < len(arr) else None

        w["rain_day"].config(text=f"{_num(dv('precipitation_sum'))} mm")
        pr = cur.get("precipitation")
        w["rain_now"].config(text=f"{_num(pr)} mm sur le dernier ¼ h" if pr else "pas de pluie en ce moment")

        p = cur.get("pressure_msl")
        w["press"].config(text=f"{_num(p)} hPa")
        p3 = hv("pressure_msl", idx - 3 if idx is not None else None)
        if p is not None and p3 is not None:
            d = p - p3
            if d <= -1:
                arrow, col = "↘", theme.RED if d <= -3 else theme.ORANGE
            elif d >= 1:
                arrow, col = "↗", theme.GREEN
            else:
                arrow, col = "→", theme.MUTED
            w["trend"].config(text=f"{arrow} {'+' if d >= 0 else ''}{_num(d)} / 3 h", fg=col)
        else:
            w["trend"].config(text="")
        if idx is not None:
            series = (hourly.get("pressure_msl") or [])[max(0, idx - 24):idx + 1]
            w["spark"].set(series)
            if series:
                w["press_sub"].config(text=f"24 h : {_num(series[0])} → {_num(series[-1])} hPa")

        uv = hv("uv_index", idx)
        lvl, col = _uv_level(uv)
        w["uv"].config(text=_num(uv, "{:.0f}") if uv is not None else "—")
        w["uv_lvl"].config(text=lvl, fg=col)
        rad = hv("shortwave_radiation", idx)
        uvmax = dv("uv_index_max")
        w["rad"].config(text=f"{_num(rad, '{:.0f}')} W/m²" + (f" · max UV {_num(uvmax, '{:.0f}')}" if uvmax is not None else ""))

        tmin, tmax = dv("temperature_2m_min"), dv("temperature_2m_max")
        w["minmax"].config(text=f"{_num(tmin)} / {_num(tmax)} °C")
        name = ""
        try:
            name = self._profile_current().get("name", "")
        except Exception:
            pass
        stamp = (cur.get("time") or "")[11:16]
        w["src"].config(text=f"Open-Meteo · {stamp}\n{name}".strip(), fg=theme.MUTED)
