"""Page « Spots HF » : qui entend vos émissions (VarAC, FT8, WSPR, Winlink…)
et celles des indicatifs que vous suivez, d'après PSKReporter (Internet,
aucune réception radio). Interrogation au plus toutes les 5 minutes, comme
le demande PSKReporter."""

import math
import threading
import time
import tkinter as tk
import xml.etree.ElementTree as ET
from tkinter import ttk

import requests

from . import gonio, theme
from .version import __version__

URL = "https://retrieve.pskreporter.info/query"
MIN_INTERVAL = 300
BANDS = ((1.8, 2.0, "160 m"), (3.5, 3.8, "80 m"), (5.25, 5.45, "60 m"), (7.0, 7.3, "40 m"), (10.1, 10.15, "30 m"),
         (14.0, 14.35, "20 m"), (18.068, 18.168, "17 m"), (21.0, 21.45, "15 m"), (24.89, 24.99, "12 m"),
         (28.0, 29.7, "10 m"), (50.0, 54.0, "6 m"), (144.0, 148.0, "2 m"))


def band_of(hz):
    try:
        mhz = float(hz) / 1e6
    except (TypeError, ValueError):
        return "?"
    for lo, hi, name in BANDS:
        if lo <= mhz <= hi:
            return name
    return f"{mhz:.3f} MHz"


def loc_to_latlon(loc):
    loc = (loc or "").strip()
    if len(loc) < 4:
        return None
    try:
        p = gonio.parse_position(loc[:6])
        return (p[0], p[1]) if p else None
    except (ValueError, TypeError, IndexError):
        return None


def haversine(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def query(call, direction, seconds):
    params = {"rronly": 1, "noactive": 1, "flowStartSeconds": -int(seconds),
              ("senderCallsign" if direction == "tx" else "receiverCallsign"): call}
    r = requests.get(URL, params=params, timeout=40,
                     headers={"User-Agent": f"STATION-SDR/{__version__} (amateur radio)"})
    if r.status_code == 503 or "too many" in r.text[:300].lower():
        raise RuntimeError("PSKReporter demande d'attendre (trop de requêtes) : réessayez dans quelques minutes")
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for rep in root.iter():
        if not rep.tag.endswith("receptionReport"):
            continue
        g = rep.attrib.get
        out.append({"t": int(g("flowStartSeconds") or 0), "tx": g("senderCallsign", ""), "tx_loc": g("senderLocator", ""),
                    "rx": g("receiverCallsign", ""), "rx_loc": g("receiverLocator", ""),
                    "freq": g("frequency"), "mode": g("mode", ""), "snr": g("sNR")})
    return out


class SpotsMixin:
    def _build_spots_tab(self, frame):
        outer = tk.Frame(frame, bg=theme.BG)
        outer.pack(fill="both", expand=True, padx=4, pady=4)
        bar = tk.Frame(outer, bg=theme.BG)
        bar.pack(fill="x", padx=12, pady=(8, 6))
        tk.Label(bar, text="Indicatifs :", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_BODY).pack(side="left")
        self.sp_calls = tk.StringVar(value=self.config_data.get("spots_calls", "F4JCF"))
        ttk.Entry(bar, textvariable=self.sp_calls, width=28).pack(side="left", padx=6)
        self.sp_dir = tk.StringVar(value="tx")
        for val, txt in (("tx", "Qui les entend"), ("rx", "Ce qu'ils entendent")):
            tk.Radiobutton(bar, text=txt, value=val, variable=self.sp_dir, command=self._sp_refresh,
                           bg=theme.BG, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG,
                           activeforeground=theme.TEXT, font=theme.FONT_BODY, highlightthickness=0).pack(side="left",
                                                                                                    padx=4)
        tk.Label(bar, text="Période :", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE).pack(side="left",
                                                                                                    padx=(12, 4))
        self.sp_period = tk.StringVar(value="6 h")
        ttk.Combobox(bar, textvariable=self.sp_period, values=("1 h", "6 h", "24 h"), width=5,
                     state="readonly").pack(side="left")
        ttk.Button(bar, text="Actualiser", style="Accent.TButton", command=self._sp_refresh).pack(side="left", padx=8)
        self.sp_status = tk.Label(bar, text="", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.sp_status.pack(side="left", padx=8)
        self.sp_summary = tk.Label(outer, text="", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION, anchor="w")
        self.sp_summary.pack(fill="x", padx=12)
        body = tk.Frame(outer, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=12, pady=(4, 8))
        left = tk.Frame(body, bg=theme.BG, width=theme.px(760))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tb = theme.card(left)
        tb.pack(fill="both", expand=True)
        cols = ("t", "tx", "rx", "loc", "dist", "band", "mode", "snr")
        self.sp_tree = ttk.Treeview(tb.inner, columns=cols, show="headings")
        for c, (title, w) in {"t": ("Heure", 70), "tx": ("Émetteur", 100), "rx": ("Récepteur", 100),
                              "loc": ("Locator", 70), "dist": ("Distance", 80), "band": ("Bande", 70),
                              "mode": ("Mode", 70), "snr": ("SNR", 50)}.items():
            self.sp_tree.heading(c, text=title)
            self.sp_tree.column(c, width=w, anchor="center")
        vsb = ttk.Scrollbar(tb.inner, orient="vertical", command=self.sp_tree.yview)
        self.sp_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.sp_tree.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(left, bg=theme.BG, fg=theme.MUTED, font=("Segoe UI", 8), justify="left", wraplength=theme.px(750),
                 text=("Données PSKReporter.info : les stations qui décodent un signal (VarAC, FT8, WSPR, JS8…) le "
                       "signalent sur Internet. Séparez plusieurs indicatifs par des virgules. Actualisation "
                       "automatique toutes les 5 minutes quand la page est ouverte.")).pack(anchor="w", pady=(4, 0))
        mo = theme.card(body)
        mo.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.sp_map = self._make_map(mo.inner)
        self.sp_map.pack(fill="both", expand=True, padx=1, pady=1)
        lat, lon = self._station_coords() if hasattr(self, "_station_coords") else (43.5905, 7.0021)
        self.sp_map.set_position(lat, lon)
        self.sp_map.set_zoom(3)
        self._sp_cache = {}
        self._sp_poll_running = False
        self._sp_objs = []

    def _sp_start_poll(self):
        if not self._sp_poll_running:
            self._sp_poll_running = True
            self._sp_poll()

    def _sp_poll(self):
        if getattr(self, "_current_view", None) != "spots":
            self._sp_poll_running = False
            return
        self._sp_refresh(auto=True)
        self.after(MIN_INTERVAL * 1000, self._sp_poll)

    def _sp_refresh(self, auto=False):
        calls = [c.strip().upper() for c in self.sp_calls.get().replace(";", ",").split(",") if c.strip()][:5]
        if not calls:
            return
        self.config_data["spots_calls"] = ", ".join(calls)
        direction = self.sp_dir.get()
        seconds = {"1 h": 3600, "6 h": 6 * 3600, "24 h": 86400}.get(self.sp_period.get(), 6 * 3600)
        self.sp_status.config(text="Interrogation de PSKReporter…", fg=theme.MUTED)

        def worker():
            spots, errs, cached = [], [], False
            for call in calls:
                key = (call, direction, seconds)
                hit = self._sp_cache.get(key)
                if hit and time.time() - hit[0] < MIN_INTERVAL:
                    spots += hit[1]
                    cached = True
                    continue
                try:
                    res = query(call, direction, seconds)
                    self._sp_cache[key] = (time.time(), res)
                    spots += res
                except Exception as exc:
                    errs.append(f"{call} : {exc}")
                    if hit:
                        spots += hit[1]
            self.after(0, self._sp_render, spots, errs, direction, cached)

        threading.Thread(target=worker, daemon=True).start()

    def _sp_render(self, spots, errs, direction, cached):
        home = self._station_coords() if hasattr(self, "_station_coords") else (43.5905, 7.0021)
        seen = set()
        rows = []
        for s in sorted(spots, key=lambda s: -s["t"]):
            k = (s["tx"], s["rx"], s["mode"], band_of(s["freq"]), s["t"] // 120)
            if k in seen:
                continue
            seen.add(k)
            other_loc = s["rx_loc"] if direction == "tx" else s["tx_loc"]
            ref_loc = s["tx_loc"] if direction == "tx" else s["rx_loc"]
            p_other, p_ref = loc_to_latlon(other_loc), loc_to_latlon(ref_loc) or home
            dist = haversine(p_ref, p_other) if p_other else None
            rows.append((s, other_loc, p_other, dist))
        tree = self.sp_tree
        tree.delete(*tree.get_children())
        for s, loc, _p, dist in rows[:500]:
            tree.insert("", "end", values=(time.strftime("%H:%M", time.localtime(s["t"])), s["tx"], s["rx"], loc,
                                           f"{dist:.0f} km" if dist is not None else "—", band_of(s["freq"]),
                                           s["mode"], s["snr"] if s["snr"] not in (None, "") else "—"))
        for o in self._sp_objs:
            try:
                o.delete()
            except Exception:
                pass
        self._sp_objs = []
        best = {}
        for s, loc, p, dist in rows:
            who = s["rx"] if direction == "tx" else s["tx"]
            if p and (who not in best or (dist or 0) > (best[who][2] or 0)):
                best[who] = (p, s, dist)
        for who, (p, s, _d) in list(best.items())[:150]:
            self._sp_objs.append(self.sp_map.set_marker(p[0], p[1], text=who, text_color="#1d4ed8",
                                                        marker_color_circle="#1d4ed8",
                                                        marker_color_outside="#60a5fa", font=("Segoe UI", 8)))
        self._sp_objs.append(self.sp_map.set_marker(home[0], home[1], text="F4JCF", text_color="#16a34a",
                                                    marker_color_circle="#14532d", marker_color_outside="#22c55e"))
        if rows:
            far = max((r for r in rows if r[3] is not None), key=lambda r: r[3], default=None)
            n = len(best)
            verb = "entendu(s) par" if direction == "tx" else "a / ont entendu"
            txt = f"{', '.join(sorted({r[0]['tx' if direction == 'tx' else 'rx'] for r in rows}))} {verb} {n} station(s)"
            if far:
                txt += f" · plus loin : {far[3]:.0f} km ({far[0]['rx' if direction == 'tx' else 'tx']}, {band_of(far[0]['freq'])})"
            self.sp_summary.config(text=txt)
        else:
            self.sp_summary.config(text="Aucun spot sur la période." + (
                " Émettez une balise (VarAC, FT8…) pour être signalé." if direction == "tx" else ""))
        if "spots" in getattr(self, "tile_chips", {}):
            self.tile_chips["spots"].config(text=f"{len(best)} station(s) · {self.sp_period.get()}")
        st = time.strftime("mis à jour %H:%M") + (" (cache)" if cached else "")
        if errs:
            self.sp_status.config(text="⚠ " + " | ".join(errs)[:160], fg=theme.ORANGE)
        else:
            self.sp_status.config(text=st, fg=theme.MUTED)
