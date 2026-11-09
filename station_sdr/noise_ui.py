"""Fenêtre « Bruit radio » : relevé quotidien du niveau de bruit par bande,
mesuré par le Pi (service meteor-api, rtl_power à gain fixe)."""

import statistics
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import requests

from . import theme

NOISE_COLORS = ("#f0b429", "#22c55e", "#06b6d4", "#c084fc", "#ef4444", "#ff7a1a", "#94a3b8")


class NoiseMixin:
    def _open_noise_window(self):
        win = getattr(self, "_noise_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            return
        win = tk.Toplevel(self)
        self._noise_win = win
        win.title("Bruit radio — relevé quotidien")
        win.configure(bg=theme.BG)
        win.geometry(f"{theme.px(1320)}x{theme.px(700)}")
        bar = tk.Frame(win, bg=theme.BG)
        bar.pack(fill="x", padx=12, pady=8)
        tk.Label(bar, text="Période :", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_BODY).pack(side="left")
        self._noise_days = 30
        for days, txt in ((7, "7 j"), (30, "30 j"), (90, "90 j"), (365, "1 an")):
            ttk.Button(bar, text=txt, style="Flat.TButton",
                       command=lambda d=days: self._noise_set_days(d)).pack(side="left", padx=3)
        self._noise_band = tk.StringVar(value="Toutes les bandes")
        self._noise_band_cb = ttk.Combobox(bar, textvariable=self._noise_band, values=["Toutes les bandes"],
                                           width=18, state="readonly")
        self._noise_band_cb.pack(side="left", padx=(12, 0))
        self._noise_band_cb.bind("<<ComboboxSelected>>", lambda _e: self._noise_draw())
        self._noise_status = tk.Label(bar, text="Chargement…", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self._noise_status.pack(side="left", padx=12)
        ttk.Button(bar, text="Mesurer maintenant", style="Accent.TButton",
                   command=self._noise_run_now).pack(side="right")
        ttk.Button(bar, text="Référence 50 Ω…", style="Flat.TButton",
                   command=lambda: self._noise_run_now(reference=True)).pack(side="right", padx=6)
        ttk.Button(bar, text="Actualiser", style="Flat.TButton",
                   command=self._noise_fetch).pack(side="right", padx=6)
        body = tk.Frame(win, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self._noise_canvas = tk.Canvas(body, bg=theme.BG_CARD, highlightthickness=0)
        self._noise_canvas.pack(side="left", fill="both", expand=True)
        self._noise_canvas.bind("<Configure>", lambda _e: self._noise_draw())
        side = tk.Frame(body, bg=theme.BG, width=theme.px(470))
        side.pack(side="left", fill="y", padx=(10, 0))
        side.pack_propagate(False)
        tk.Label(side, text="Dernier relevé nocturne", bg=theme.BG, fg=theme.TEXT,
                 font=theme.FONT_SECTION).pack(anchor="w")
        cols = ("band", "med", "delta", "r50", "marge")
        tree = ttk.Treeview(side, columns=cols, show="headings", height=7)
        for c, (t, w) in {"band": ("Bande", 130), "med": ("Bruit", 70), "delta": ("Écart", 70),
                          "r50": ("Clé 50 Ω", 70), "marge": ("Marge", 70)}.items():
            tree.heading(c, text=t)
            tree.column(c, width=theme.px(w), anchor="center")
        tree.tag_configure("up", foreground=theme.RED)
        tree.tag_configure("ok", foreground=theme.GREEN)
        tree.tag_configure("key", foreground=theme.MUTED)
        tree.pack(fill="x", pady=(4, 8))
        self._noise_tree = tree
        tk.Label(side, justify="left", wraplength=theme.px(320), bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE,
                 text=("Chaque nuit (03:30 par défaut), le Pi prend la clé SDR deux minutes et mesure le bruit "
                       "de fond (médiane, en dB relatifs, gain fixe) sur chaque bande, puis rend la clé.\n\n"
                       "Les valeurs ne sont comparables qu'à elles-mêmes : c'est l'évolution qui compte. "
                       "Un écart de plus de 3 dB au-dessus de la médiane des relevés nocturnes des 30 jours "
                       "précédents (en rouge) signale une nouvelle source de brouillage (alimentation, LED, "
                       "chargeur, box…) ou un souci d'antenne ou de câble. Les mesures lancées à la main "
                       "(points creux) n'entrent pas dans cette médiane.\n\n"
                       "« Clé 50 Ω » : bruit propre de la clé, mesuré une fois avec une charge à la place de "
                       "l'antenne. « Marge » = bruit habituel − bruit de la clé : moins de 2 dB (en gris), la "
                       "bande n'entend pas l'environnement.\n\n"
                       "Aucun relevé pendant la veille 121,5 MHz ni pendant un passage de satellite.")).pack(anchor="w")
        self._noise_data = None
        self._noise_fetch()

    def _noise_set_days(self, days):
        self._noise_days = days
        self._noise_draw()

    def _noise_fetch(self):
        base = self._mt_base()

        def worker():
            try:
                r = requests.get(base + "/api/meteor/noise", timeout=8)
                r.raise_for_status()
                data, err = r.json(), None
            except Exception as exc:
                data, err = None, str(exc)
            self.after(0, self._noise_loaded, data, err)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------ données
    def _noise_auto(self, e):
        """Relevé nocturne automatique (référence) ou mesure lancée à la main ?
        Les anciens relevés ne portent pas l'indication : on se fie à l'heure."""
        if e.get("manual") or e.get("reference"):
            return False
        nt = (self._noise_data or {}).get("noise_time") or "03:30"
        try:
            hh, mm = (int(v) for v in nt.split(":"))
        except ValueError:
            hh, mm = 3, 30
        lt = time.localtime(e["time"])
        d = abs((lt.tm_hour * 60 + lt.tm_min) - (hh * 60 + mm))
        return min(d, 1440 - d) <= 90

    def _noise_ref_median(self, band, before=None):
        """Médiane des relevés nocturnes des 30 jours (avant `before`)."""
        hist = (self._noise_data or {}).get("history") or []
        end = before if before is not None else time.time() + 1
        vals = [e["bands"][band]["median"] for e in hist
                if self._noise_auto(e) and end - 30 * 86400 <= e["time"] < end and band in e["bands"]]
        return statistics.median(vals) if vals else None

    def _noise_ref50(self, band):
        ref = (self._noise_data or {}).get("reference") or []
        for e in reversed(ref):
            if band in e.get("bands", {}):
                return e["bands"][band]["median"], e["time"]
        return None, None

    @staticmethod
    def _fmt_db(v, signed=False):
        v = round(v, 1)
        if v == 0:                        # jamais « -0.0 dB »
            return "0.0 dB"
        return f"{v:+.1f} dB" if signed else f"{v:.1f} dB"

    def _noise_loaded(self, data, err):
        if not getattr(self, "_noise_win", None) or not self._noise_win.winfo_exists():
            return
        if err:
            self._noise_status.config(text=("Relevé indisponible : installez le module « Images satellites » "
                                             "puis mettez à jour le Pi."))
            return
        self._noise_data = data
        hist = data.get("history") or []
        autos = [e for e in hist if self._noise_auto(e)]
        bands = data.get("bands") or []
        self._noise_band_cb.config(values=["Toutes les bandes"] + bands)
        pending = {"manual": " · mesure demandée, en attente", "reference": " · référence 50 Ω demandée, en attente"}
        self._noise_status.config(
            text=((f"{len(autos)} relevé(s) nocturne(s), {len(hist) - len(autos)} à la main · dernier relevé "
                   f"nocturne le {time.strftime('%d/%m/%Y à %H:%M', time.localtime(autos[-1]['time']))}")
                  if autos else "Pas encore de relevé nocturne : le premier aura lieu cette nuit.")
            + pending.get(data.get("pending"), ""))
        tree = self._noise_tree
        tree.delete(*tree.get_children())
        last = autos[-1] if autos else None
        for band in bands:
            v = (last["bands"].get(band) or {}).get("median") if last else None
            ref = self._noise_ref_median(band, before=last["time"]) if last else None
            r50, _t = self._noise_ref50(band)
            cells = [band, self._fmt_db(v) if v is not None else "—",
                     self._fmt_db(v - ref, signed=True) if v is not None and ref is not None else "—",
                     self._fmt_db(r50) if r50 is not None else "—",
                     self._fmt_db(v - r50, signed=True) if v is not None and r50 is not None else "—"]
            tags = ()
            if v is not None and r50 is not None and v - r50 < 2:
                tags = ("key",)
            elif v is not None and ref is not None:
                tags = ("up",) if v - ref > 3 else ("ok",)
            tree.insert("", "end", values=cells, tags=tags)
        self._noise_draw()

    # ---------------------------------------------------------- graphique
    def _noise_draw(self):
        cv = getattr(self, "_noise_canvas", None)
        if cv is None or not cv.winfo_exists():
            return
        cv.delete("all")
        w, h = max(300, cv.winfo_width()), max(200, cv.winfo_height())
        data = self._noise_data or {}
        now = time.time()
        # Période alignée sur minuit : du minuit d'il y a N jours au prochain minuit
        lt = time.localtime(now)
        next_midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
        t1 = next_midnight
        t0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1 - self._noise_days, 0, 0, 0, 0, 0, -1))
        hist = [x for x in (data.get("history") or []) if t0 <= x["time"] <= t1]
        all_bands = data.get("bands") or []
        sel = self._noise_band.get()
        bands = all_bands if sel not in all_bands else [sel]
        cv.create_text(14, 12, anchor="nw", text="Bruit de fond par bande (médiane, dB relatifs)",
                       fill=theme.TEXT, font=theme.FONT_BODY_BOLD)
        if not hist:
            cv.create_text(w / 2, h / 2, text="Aucun relevé sur la période", fill=theme.MUTED, font=theme.FONT_BODY)
            return
        vals = [x["bands"][b]["median"] for x in hist for b in bands if b in x["bands"]]
        single = len(bands) == 1          # zone ±3 dB et référence 50 Ω : une bande à la fois (lisibilité)
        refs = {b: self._noise_ref_median(b) for b in bands} if single else {}
        r50s = {b: self._noise_ref50(b)[0] for b in bands} if single else {}
        vals += [r + d for r in refs.values() if r is not None for d in (-3, 3)]
        vals += [r for r in r50s.values() if r is not None]
        if not vals:
            return
        lo, hi = min(vals) - 1, max(vals) + 1
        if hi - lo < 6:
            mid = (hi + lo) / 2
            lo, hi = mid - 3, mid + 3
        x0, y0, x1, y1 = 60, 44, w - 16, h - 70
        sx = lambda t: x0 + (t - t0) / (t1 - t0) * (x1 - x0)
        sy = lambda v: y1 - (v - lo) / (hi - lo) * (y1 - y0)
        small = ("Segoe UI", theme.fs(8))
        # Graduations : une par jour, à minuit ; étiquettes espacées si la place manque
        day_px = (x1 - x0) / self._noise_days
        every = 1
        while day_px * every < 44:
            every += 1
        for k in range(self._noise_days + 1):
            t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1 - self._noise_days + k, 0, 0, 0, 0, 0, -1))
            x = sx(t)
            cv.create_line(x, y0, x, y1, fill=theme.BG_CARD_ALT)
            cv.create_line(x, y1, x, y1 + 4, fill=theme.MUTED)
            if k % every == 0 and k < self._noise_days:
                cv.create_text(x + 2, y1 + 14, anchor="w", text=time.strftime("%d/%m", time.localtime(t)),
                               fill=theme.MUTED, font=small)
        for i in range(6):
            v = lo + (hi - lo) * i / 5
            cv.create_line(x0, sy(v), x1, sy(v), fill=theme.BG_CARD_ALT, dash=(2, 4))
            cv.create_text(x0 - 6, sy(v), anchor="e", text=f"{v:.0f}", fill=theme.MUTED, font=small)
        for band in bands:
            color = NOISE_COLORS[all_bands.index(band) % len(NOISE_COLORS)]
            ref = refs.get(band)
            if ref is not None:          # zone de ±3 dB autour de la médiane des relevés nocturnes
                cv.create_rectangle(x0, sy(ref + 3), x1, sy(ref - 3), fill=color, outline="",
                                    stipple="gray12")
                cv.create_line(x0, sy(ref), x1, sy(ref), fill=color, dash=(6, 4))
            r50 = r50s.get(band)
            if r50 is not None:          # bruit propre de la clé (charge 50 Ω)
                cv.create_line(x0, sy(r50), x1, sy(r50), fill=color, dash=(1, 3), width=2)
                cv.create_text(x1 - 4, sy(r50) - 8, anchor="e", text="clé seule (charge 50 Ω)", fill=color,
                               font=small)
        for band in bands:
            color = NOISE_COLORS[all_bands.index(band) % len(NOISE_COLORS)]
            auto = [(sx(x["time"]), sy(x["bands"][band]["median"])) for x in hist
                    if band in x["bands"] and self._noise_auto(x)]
            if len(auto) > 1:
                cv.create_line(*[c for p in auto for c in p], fill=color, width=2)
            for px, py in auto:                       # relevés nocturnes : points pleins
                cv.create_oval(px - 3, py - 3, px + 3, py + 3, fill=color, outline=color)
            for x in hist:                            # mesures à la main : points creux, hors courbe
                if band in x["bands"] and not self._noise_auto(x):
                    px, py = sx(x["time"]), sy(x["bands"][band]["median"])
                    cv.create_oval(px - 4, py - 4, px + 4, py + 4, fill=theme.BG_CARD, outline=color, width=2)
        # Légende
        for bi, band in enumerate(all_bands):
            color = NOISE_COLORS[bi % len(NOISE_COLORS)]
            lx = x0 + (bi % 4) * theme.px(190)
            ly = h - 40 + (bi // 4) * 16
            dim = band not in bands
            cv.create_rectangle(lx, ly - 4, lx + 14, ly + 4, fill=theme.BG_CARD_ALT if dim else color, outline=color)
            cv.create_text(lx + 20, ly, anchor="w", text=band, fill=theme.MUTED if dim else theme.TEXT,
                           font=("Segoe UI", theme.fs(9)))
        cv.create_text(x1, h - 8, anchor="e", fill=theme.MUTED, font=small,
                       text=("● relevé nocturne   ○ mesure à la main (hors médiane)   ░ ±3 dB autour de la médiane   "
                             "┄ médiane   ··· clé seule (50 Ω)") if single else
                       ("● relevé nocturne   ○ mesure à la main (hors médiane)   — choisissez une bande en haut pour "
                        "voir la zone ±3 dB et la référence 50 Ω"))

    # ------------------------------------------------------------ mesures
    def _noise_run_now(self, reference=False):
        if reference and not messagebox.askokcancel(
                "Référence 50 Ω",
                "Mesure du bruit propre de la clé :\n\n"
                "1. Débranchez l'antenne de la clé SDR et vissez à la place une charge 50 Ω (bouchon de charge).\n"
                "2. Cliquez OK : la mesure démarre dans la minute et dure environ deux minutes.\n"
                "3. Attendez « Actualiser » (environ 3 minutes), puis REBRANCHEZ L'ANTENNE.\n\n"
                "Les bandes dont le bruit habituel dépasse à peine cette référence (moins de 2 dB, en gris dans le "
                "tableau) n'entendent que la clé, pas l'environnement.", parent=self._noise_win):
            return
        base = self._mt_base()

        def worker():
            try:
                r = requests.post(base + "/api/meteor/noise/run", json={"reference": True} if reference else {},
                                  timeout=8)
                r.raise_for_status()
                msg = ("Mesure de référence demandée. Pensez à rebrancher l'antenne dans environ 3 minutes."
                       if reference else
                       "Mesure demandée : elle démarre dans la minute (sauf veille 121,5 MHz ou passage de "
                       "satellite imminent) et dure environ deux minutes. Elle apparaîtra en point creux, sans "
                       "compter dans la médiane de référence.")
            except Exception as exc:
                msg = f"Demande refusée : {exc}"
            self.after(0, lambda: messagebox.showinfo("Bruit radio", msg, parent=self._noise_win))
            self.after(4000, self._noise_fetch)

        threading.Thread(target=worker, daemon=True).start()
