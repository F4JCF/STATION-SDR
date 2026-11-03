"""Foudre sur la page Météo : impacts récents sur la carte, distance du plus
proche, et alerte quand un orage approche de la station."""

import time
import tkinter as tk

from PIL import Image, ImageDraw, ImageTk

from . import lightning, theme
from .config import save_config


def _dot(color, size=10):
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([1, 1, size - 2, size - 2], fill=color, outline=(0, 0, 0, 200))
    return ImageTk.PhotoImage(im)


class LightningMixin:
    def _build_lightning_row(self, buttons_row, info_parent):
        cb = dict(bg=theme.BG, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG,
                  activeforeground=theme.TEXT, font=theme.FONT_BODY, highlightthickness=0)
        self.lt_show = tk.BooleanVar(value=bool(self.config_data.get("lightning_show", True)))
        tk.Checkbutton(buttons_row, text="⚡ Foudre", variable=self.lt_show, command=self._lt_toggle,
                       **cb).pack(side="left", padx=(12, 0))
        self.lt_alert = tk.BooleanVar(value=bool(self.config_data.get("lightning_alert", True)))
        tk.Checkbutton(buttons_row, text="alerte <", variable=self.lt_alert, command=self._lt_save,
                       **cb).pack(side="left", padx=(6, 0))
        self.lt_km = tk.StringVar(value=str(self.config_data.get("lightning_km", 25)))
        tk.Spinbox(buttons_row, from_=5, to=100, increment=5, textvariable=self.lt_km, width=4, command=self._lt_save,
                   bg=theme.BG_CARD_ALT, fg=theme.TEXT, buttonbackground=theme.BG_CARD_ALT,
                   insertbackground=theme.TEXT, relief="flat").pack(side="left", padx=2)
        tk.Label(buttons_row, text="km", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_BODY).pack(side="left")
        self.lt_info = tk.Label(info_parent, text="", bg=theme.BG, fg=theme.MUTED, font=("Segoe UI", 8),
                                wraplength=640, justify="left")
        self.lt_info.pack(anchor="w", pady=(2, 0))
        self._lt_icons = {}
        self._lt_markers = []
        lat, lon = self._station_coords() if hasattr(self, "_station_coords") else (43.5905, 7.0021)
        self._lt = lightning.Lightning(lat, lon, on_strike=self._lt_on_strike)
        self._lt.start()
        self.after(5000, self._lt_tick)

    def _lt_save(self):
        self.config_data["lightning_show"] = bool(self.lt_show.get())
        self.config_data["lightning_alert"] = bool(self.lt_alert.get())
        try:
            self.config_data["lightning_km"] = max(5, min(100, int(self.lt_km.get())))
        except ValueError:
            pass
        try:
            save_config(self.config_data)
        except Exception:
            pass

    def _lt_toggle(self):
        self._lt_save()
        self._lt_draw()

    def _lt_on_strike(self, s):
        """Appelé par le fil de réception : alerte si l'impact est proche."""
        if not self.config_data.get("lightning_alert", True):
            return
        km = float(self.config_data.get("lightning_km", 25))
        if s[3] <= km:
            self.after(0, self._raise_alert, "foudre", f"Orage à {s[3]:.0f} km de la station",
                       f"Impact de foudre à {s[3]:.0f} km. Pensez à débrancher les antennes et à protéger le "
                       f"matériel si l'orage se rapproche.", 1800)

    def _lt_tick(self):
        try:
            self._lt_draw()
        finally:
            self.after(30000, self._lt_tick)

    def _lt_draw(self):
        if not hasattr(self, "weather_map"):
            return
        near = self._lt.recent(1800, 300)
        n100 = [s for s in near if s[3] <= 100]
        if near:
            closest = min(near, key=lambda s: s[3])
            txt = (f"{len(n100)} impact(s) à moins de 100 km en 30 min · le plus proche : {closest[3]:.0f} km, "
                   f"il y a {max(0, (time.time() - closest[0]) / 60):.0f} min")
        else:
            txt = "Aucun impact à moins de 300 km depuis 30 min."
        self.lt_info.config(text=f"{txt}\n{self._lt.status} — données Blitzortung.org",
                            fg=theme.ORANGE if n100 else theme.MUTED)
        if "weather" in getattr(self, "tile_chips", {}) and n100:
            cur = self.tile_chips["weather"].text.split("  ·  ⚡")[0]
            self.tile_chips["weather"].config(text=f"{cur}  ·  ⚡ {min(s[3] for s in n100):.0f} km")
        for m in self._lt_markers:
            try:
                m.delete()
            except Exception:
                pass
        self._lt_markers = []
        if not self.lt_show.get():
            return
        if not self._lt_icons:
            self._lt_icons = {"new": _dot((255, 255, 255, 255)), "mid": _dot((250, 204, 21, 255)),
                              "old": _dot((239, 68, 68, 230))}
        now = time.time()
        for t, lat, lon, _km in near[-400:]:
            age = now - t
            icon = self._lt_icons["new" if age < 300 else "mid" if age < 900 else "old"]
            try:
                self._lt_markers.append(self.weather_map.set_marker(lat, lon, icon=icon))
            except Exception:
                break
