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
        win.geometry("1100x640")
        bar = tk.Frame(win, bg=theme.BG)
        bar.pack(fill="x", padx=12, pady=8)
        tk.Label(bar, text="Période :", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_BODY).pack(side="left")
        self._noise_days = 30
        for days, txt in ((7, "7 j"), (30, "30 j"), (90, "90 j"), (365, "1 an")):
            ttk.Button(bar, text=txt, style="Flat.TButton",
                       command=lambda d=days: self._noise_set_days(d)).pack(side="left", padx=3)
        self._noise_status = tk.Label(bar, text="Chargement…", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self._noise_status.pack(side="left", padx=12)
        ttk.Button(bar, text="Mesurer maintenant", style="Accent.TButton",
                   command=self._noise_run_now).pack(side="right")
        ttk.Button(bar, text="Actualiser", style="Flat.TButton",
                   command=self._noise_fetch).pack(side="right", padx=6)
        body = tk.Frame(win, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self._noise_canvas = tk.Canvas(body, bg=theme.BG_CARD, highlightthickness=0)
        self._noise_canvas.pack(side="left", fill="both", expand=True)
        self._noise_canvas.bind("<Configure>", lambda _e: self._noise_draw())
        side = tk.Frame(body, bg=theme.BG, width=theme.px(330))
        side.pack(side="left", fill="y", padx=(10, 0))
        side.pack_propagate(False)
        tk.Label(side, text="Dernier relevé", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION).pack(anchor="w")
        cols = ("band", "med", "delta")
        tree = ttk.Treeview(side, columns=cols, show="headings", height=8)
        for c, (t, w) in {"band": ("Bande", 150), "med": ("Bruit", 70), "delta": ("Écart", 80)}.items():
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="center")
        tree.tag_configure("up", foreground=theme.RED)
        tree.tag_configure("ok", foreground=theme.GREEN)
        tree.pack(fill="x", pady=(4, 8))
        self._noise_tree = tree
        tk.Label(side, justify="left", wraplength=theme.px(320), bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE,
                 text=("Chaque nuit (03:30 par défaut), le Pi prend la clé SDR deux minutes et mesure le bruit "
                       "de fond (médiane, en dB relatifs, gain fixe) sur chaque bande, puis rend la clé.\n\n"
                       "Les valeurs ne sont comparables qu'à elles-mêmes : c'est l'évolution qui compte. "
                       "Un écart de plus de 3 dB au-dessus de la médiane des 30 jours précédents (en rouge) "
                       "signale une nouvelle source de brouillage (alimentation, LED, chargeur, box…) ou un "
                       "souci d'antenne ou de câble.\n\n"
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

    def _noise_loaded(self, data, err):
        if not getattr(self, "_noise_win", None) or not self._noise_win.winfo_exists():
            return
        if err:
            self._noise_status.config(text=("Relevé indisponible : installez le module « Images satellites » "
                                             "puis mettez à jour le Pi."))
            return
        self._noise_data = data
        hist = data.get("history") or []
        self._noise_status.config(
            text=(f"{len(hist)} relevé(s) · dernier le "
                  f"{time.strftime('%d/%m/%Y à %H:%M', time.localtime(hist[-1]['time']))}") if hist
            else "Pas encore de relevé : le premier aura lieu cette nuit (ou « Mesurer maintenant »).")
        tree = self._noise_tree
        tree.delete(*tree.get_children())
        if hist:
            last = hist[-1]
            month_ago = last["time"] - 30 * 86400
            for band in data.get("bands") or []:
                v = (last["bands"].get(band) or {}).get("median")
                if v is None:
                    continue
                prev = [h["bands"][band]["median"] for h in hist[:-1]
                        if h["time"] >= month_ago and band in h["bands"]]
                if prev:
                    d = v - statistics.median(prev)
                    tree.insert("", "end", values=(band, f"{v:.1f} dB", f"{d:+.1f} dB"),
                                tags=("up",) if d > 3 else ("ok",))
                else:
                    tree.insert("", "end", values=(band, f"{v:.1f} dB", "—"))
        self._noise_draw()

    def _noise_draw(self):
        cv = getattr(self, "_noise_canvas", None)
        if cv is None or not cv.winfo_exists():
            return
        cv.delete("all")
        w, h = max(300, cv.winfo_width()), max(200, cv.winfo_height())
        data = self._noise_data or {}
        hist = [x for x in (data.get("history") or []) if x["time"] >= time.time() - self._noise_days * 86400]
        bands = data.get("bands") or []
        cv.create_text(14, 12, anchor="nw", text="Bruit de fond par bande (médiane, dB relatifs)",
                       fill=theme.TEXT, font=theme.FONT_BODY_BOLD)
        if not hist:
            cv.create_text(w / 2, h / 2, text="Aucun relevé sur la période", fill=theme.MUTED, font=theme.FONT_BODY)
            return
        vals = [b["median"] for x in hist for b in x["bands"].values()]
        lo, hi = min(vals) - 1, max(vals) + 1
        if hi - lo < 6:
            mid = (hi + lo) / 2
            lo, hi = mid - 3, mid + 3
        x0, y0, x1, y1 = 60, 44, w - 16, h - 64
        t0 = time.time() - self._noise_days * 86400
        t1 = time.time()
        sx = lambda t: x0 + (t - t0) / (t1 - t0) * (x1 - x0)
        sy = lambda v: y1 - (v - lo) / (hi - lo) * (y1 - y0)
        for i in range(6):
            v = lo + (hi - lo) * i / 5
            cv.create_line(x0, sy(v), x1, sy(v), fill=theme.BG_CARD_ALT)
            cv.create_text(x0 - 6, sy(v), anchor="e", text=f"{v:.0f}", fill=theme.MUTED, font=("Segoe UI", 8))
        for i in range(6):
            t = t0 + (t1 - t0) * i / 5
            cv.create_text(sx(t), y1 + 12, text=time.strftime("%d/%m", time.localtime(t)), fill=theme.MUTED,
                           font=("Segoe UI", 8))
        for bi, band in enumerate(bands):
            color = NOISE_COLORS[bi % len(NOISE_COLORS)]
            pts = [(sx(x["time"]), sy(x["bands"][band]["median"])) for x in hist if band in x["bands"]]
            if len(pts) > 1:
                cv.create_line(*[c for p in pts for c in p], fill=color, width=2)
            for px, py in pts:
                cv.create_oval(px - 2, py - 2, px + 2, py + 2, fill=color, outline=color)
            lx = x0 + (bi % 4) * 190
            ly = h - 34 + (bi // 4) * 16
            cv.create_rectangle(lx, ly - 4, lx + 14, ly + 4, fill=color, outline=color)
            cv.create_text(lx + 20, ly, anchor="w", text=band, fill=theme.TEXT, font=("Segoe UI", 9))

    def _noise_run_now(self):
        base = self._mt_base()

        def worker():
            try:
                r = requests.post(base + "/api/meteor/noise/run", timeout=8)
                r.raise_for_status()
                msg = ("Relevé demandé : il démarre dans la minute (sauf veille 121,5 MHz ou passage de "
                       "satellite imminent) et dure environ deux minutes. Cliquez « Actualiser » ensuite.")
            except Exception as exc:
                msg = f"Demande refusée : {exc}"
            self.after(0, lambda: messagebox.showinfo("Bruit radio", msg, parent=self._noise_win))

        threading.Thread(target=worker, daemon=True).start()
