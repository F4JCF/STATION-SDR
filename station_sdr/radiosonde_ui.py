"""Page « Radiosondes » : ballons météo suivis par radiosonde_auto_rx
(conteneur Docker sur le Pi, interface web sur le port 5000)."""

import threading
import time
import tkinter as tk
from tkinter import ttk

import requests

from . import gonio, theme

STATION = (43.590541, 7.002072)


class RadiosondeMixin:
    def _rs_base(self):
        return f"http://{self.config_data.get('pi_host', '192.168.1.229')}:{self.config_data.get('port_radiosonde', 5000)}"

    def _build_radiosonde_tab(self, frame):
        outer = tk.Frame(frame, bg=theme.BG)
        outer.pack(fill="both", expand=True, padx=4, pady=4)
        left = tk.Frame(outer, bg=theme.BG, width=theme.px(820))
        left.pack(side="left", fill="y", pady=(0, 6))
        left.pack_propagate(False)
        c = theme.card(left)
        c.pack(fill="x", padx=12, pady=(8, 6))
        h = c.inner
        h.configure(padding=12)
        r = tk.Frame(h, bg=theme.BG_CARD)
        r.pack(fill="x")
        tk.Label(r, text="Radiosondes météo", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION).pack(side="left")
        self.rs_pill = theme.pill(r, "INCONNU", theme.MUTED)
        self.rs_pill.pack(side="left", padx=(12, 0))
        ttk.Button(r, text="Interface auto_rx", style="Flat.TButton",
                   command=lambda: self._open_url(self._rs_base() + "/")).pack(side="right")
        ttk.Button(r, text="SondeHub (carte mondiale)", style="Flat.TButton",
                   command=lambda: self._open_url("https://tracker.sondehub.org/")).pack(side="right", padx=6)
        ttk.Button(r, text="Écouter", style="Accent.TButton",
                   command=lambda: self._switch_sdr("radiosonde")).pack(side="right", padx=(0, 6))
        self.rs_info = tk.Label(h, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE,
                                justify="left", anchor="w", wraplength=theme.px(780))
        self.rs_info.pack(anchor="w", pady=(6, 0))

        tk.Label(left, text="Sondes reçues", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION).pack(
            anchor="w", padx=12, pady=(4, 4))
        tb = theme.card(left)
        tb.pack(fill="x", padx=12)
        cols = ("serial", "type", "freq", "alt", "vz", "dist", "last", "met")
        self.rs_tree = ttk.Treeview(tb.inner, columns=cols, show="headings", height=8)
        for cname, (t, w) in {"serial": ("Numéro", 110), "type": ("Type", 70), "freq": ("MHz", 70),
                              "alt": ("Altitude", 85), "vz": ("Vit. vert.", 90), "dist": ("Distance", 130),
                              "last": ("Dernière réc.", 95), "met": ("T / Hum.", 120)}.items():
            self.rs_tree.heading(cname, text=t)
            self.rs_tree.column(cname, width=w, anchor="center")
        self.rs_tree.tag_configure("down", foreground="#f59e0b")
        self.rs_tree.pack(fill="x", padx=1, pady=1)
        self.rs_tree.bind("<<TreeviewSelect>>", self._rs_select)
        d = theme.card(left)
        d.pack(fill="both", expand=True, padx=12, pady=(6, 6))
        self.rs_detail = tk.Label(d.inner, text="Sélectionnez une sonde.", bg=theme.BG_CARD, fg=theme.TEXT,
                                  font=theme.FONT_BODY, justify="left", anchor="nw", wraplength=theme.px(780), padx=12, pady=10)
        self.rs_detail.pack(fill="both", expand=True)
        tk.Label(left, text=(
            "Les radiosondes sont lâchées deux fois par jour (vers 00 h et 12 h UTC) par Météo-France, "
            "notamment à Nîmes et Ajaccio, et montent jusqu'à 30 km avant d'éclater et de redescendre sous "
            "parachute. auto_rx balaie 400–406 MHz, décode leur position et partage les données sur SondeHub."),
            bg=theme.BG, fg=theme.MUTED, font=("Segoe UI", theme.fs(8)), justify="left", wraplength=theme.px(800)).pack(
            anchor="w", padx=12, pady=(0, 6))

        right = tk.Frame(outer, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True, pady=(8, 12), padx=(0, 12))
        mc = theme.card(right)
        mc.pack(fill="both", expand=True)
        self.rs_map = self._make_map(mc.inner)
        self.rs_map.pack(fill="both", expand=True, padx=1, pady=1)
        self.rs_map.set_position(43.4, 6.0)
        self.rs_map.set_zoom(7)
        self._rs_sondes = {}
        self._rs_tracks = {}
        self._rs_sel = None
        self._rs_objs = []
        self._rs_poll_running = False

    def _rs_start_poll(self):
        if not getattr(self, "_rs_poll_running", False):
            self._rs_poll_running = True
            self._rs_poll()

    def _rs_poll(self):
        if getattr(self, "_current_view", None) != "radiosonde":
            self._rs_poll_running = False
            return
        threading.Thread(target=self._rs_fetch, daemon=True).start()
        self.after(5000, self._rs_poll)

    def _rs_fetch(self):
        base = self._rs_base()
        try:
            tasks = requests.get(base + "/get_task_list", timeout=4).json()
            telem = requests.get(base + "/get_telemetry_archive", timeout=6).json()
            err = None
        except Exception as exc:
            tasks = telem = None
            err = str(exc)
        self.after(0, self._rs_render, tasks, telem, err)

    def _rs_fetch_bg(self):
        """Rafraîchissement général : alerte « sonde qui descend près de chez vous »."""
        if getattr(self, "_sdr_mode", None) != "radiosonde" or getattr(self, "_current_view", None) == "radiosonde":
            return
        try:
            telem = requests.get(self._rs_base() + "/get_telemetry_archive", timeout=6).json()
        except Exception:
            return
        self.after(0, self._rs_alerts, telem)

    def _rs_alerts(self, telem):
        for serial, s in (telem or {}).items():
            t = s.get("latest_telem") or {}
            if t.get("lat") is None:
                continue
            dist, brg = gonio.dist_bearing(STATION[0], STATION[1], t["lat"], t["lon"])
            if (t.get("vel_v") or 0) < -3 and (t.get("alt") or 1e9) < 5000 and dist < 40:
                self._raise_alert(f"rs:{serial}", f"Radiosonde {serial} en descente à {dist:.0f} km",
                                  f"Altitude {t['alt']:.0f} m, {t.get('vel_v'):.1f} m/s · au {brg:.0f}° "
                                  f"({self._compass(brg)}) de la station\nPosition : {t['lat']:.5f}, {t['lon']:.5f}",
                                  cooldown=3600)
        if "radiosonde" in self.tile_chips:
            n = sum(1 for s in (telem or {}).values()
                    if time.time() - self._rs_age(s) < 1800)
            self.tile_chips["radiosonde"].config(text=f"{n} sonde(s) suivie(s)" if n else "aucune sonde en vol")

    @staticmethod
    def _rs_age(s):
        """Horodatage (epoch) de la dernière trame d'une sonde (entrée de l'archive auto_rx)."""
        v = s.get("timestamp")
        return v if isinstance(v, (int, float)) else 0

    def _rs_render(self, tasks, telem, err):
        if not hasattr(self, "rs_tree"):
            return
        mode = getattr(self, "_sdr_mode", None)
        labels = getattr(self, "SDR_LABELS", {}) or {}
        if err:
            if mode not in (None, "radiosonde"):
                self.rs_pill.config(text="  EN PAUSE  " if mode != "off" else "  CLÉ ARRÊTÉE  ", bg=theme.MUTED)
                self.rs_info.config(text=f"La clé SDR est sur {labels.get(mode, mode)}. Cliquez « Écouter ».",
                                    fg=theme.MUTED)
            else:
                self.rs_pill.config(text="  NON INSTALLÉ / DÉMARRAGE  ", bg=theme.MUTED)
                self.rs_info.config(text=("auto_rx (port 5000) ne répond pas : il démarre (30 s), ou n'est pas "
                                          "installé (Santé du Pi → « Modules du Pi… »)."), fg=theme.MUTED)
            return
        sdr = (tasks or {}).get("0") or next(iter((tasks or {}).values()), {}) if tasks else {}
        task = sdr.get("task", "—") if isinstance(sdr, dict) else "—"
        task = task.replace("Scanning", "recherche de sondes (400–406 MHz)").replace("Decoding", "décodage") \
            .replace("Not Tasked", "en attente")
        self.rs_pill.config(text="  ÉCOUTE  ", bg=theme.GREEN)
        self.rs_info.config(text=f"auto_rx : {task}", fg=theme.MUTED)
        self._rs_sondes = telem or {}
        self._rs_alerts(telem)
        now = time.time()
        for serial, s in self._rs_sondes.items():
            path = [(p[0], p[1]) for p in (s.get("path") or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
            self._rs_tracks[serial] = path[-3000:]
        tree = self.rs_tree
        tree.delete(*tree.get_children())
        for serial, s in sorted(self._rs_sondes.items(), key=lambda kv: -self._rs_age(kv[1])):
            t = s.get("latest_telem") or {}
            dist = ""
            if t.get("lat") is not None:
                dkm, brg = gonio.dist_bearing(STATION[0], STATION[1], t["lat"], t["lon"])
                dist = f"{dkm:.0f} km · {brg:.0f}° {self._compass(brg)}"
            age = now - self._rs_age(s)
            vz = t.get("vel_v")
            met = []
            if isinstance(t.get("temp"), (int, float)) and t["temp"] > -270:
                met.append(f"{t['temp']:.0f} °C")
            if isinstance(t.get("humidity"), (int, float)) and t["humidity"] >= 0:
                met.append(f"{t['humidity']:.0f} %")
            tree.insert("", "end", iid=serial, tags=("down",) if (vz or 0) < -2 else (), values=(
                serial, t.get("type", ""), str(t.get("freq", "")).replace(" MHz", ""),
                f"{t.get('alt', 0):.0f} m", f"{vz:+.1f} m/s" if isinstance(vz, (int, float)) else "",
                dist, f"il y a {int(age // 60)} min" if age >= 60 else "maintenant", " · ".join(met)))
        if self._rs_sel in self._rs_sondes:
            tree.selection_set(self._rs_sel)
        self._rs_draw_map()

    def _rs_select(self, _e=None):
        sel = self.rs_tree.selection()
        if not sel:
            return
        self._rs_sel = sel[0]
        t = (self._rs_sondes.get(sel[0]) or {}).get("latest_telem") or {}
        if t.get("lat") is None:
            return
        dkm, brg = gonio.dist_bearing(STATION[0], STATION[1], t["lat"], t["lon"])
        state = "en descente sous parachute" if (t.get("vel_v") or 0) < -2 else "en montée"
        self.rs_detail.config(text=(
            f"{sel[0]} · {t.get('type', '')} {t.get('subtype', '')} · {state}\n"
            f"Position {t['lat']:.5f}, {t['lon']:.5f} · altitude {t.get('alt', 0):.0f} m · "
            f"{dkm:.1f} km au {brg:.0f}° ({self._compass(brg)}) de la station\n"
            f"Vitesse verticale {t.get('vel_v', 0):+.1f} m/s · horizontale {t.get('vel_h', 0) * 3.6:.0f} km/h · "
            f"cap {t.get('heading', 0):.0f}°\n"
            f"Pression {t.get('pressure', 0):.0f} hPa · température {t.get('temp', 0):.1f} °C · "
            f"humidité {t.get('humidity', 0):.0f} %"))
        self.rs_map.set_position(t["lat"], t["lon"])

    def _rs_draw_map(self):
        for o in self._rs_objs:
            try:
                o.delete()
            except Exception:
                pass
        self._rs_objs = [self.rs_map.set_marker(*STATION, text="F4JCF", text_color="#16a34a",
                                                marker_color_circle="#14532d", marker_color_outside="#22c55e")]
        for serial, s in self._rs_sondes.items():
            t = s.get("latest_telem") or {}
            if t.get("lat") is None:
                continue
            down = (t.get("vel_v") or 0) < -2
            col = "#f59e0b" if down else "#38bdf8"
            tr = self._rs_tracks.get(serial) or []
            if len(tr) >= 2:
                self._rs_objs.append(self.rs_map.set_path(tr, color=col, width=2))
            self._rs_objs.append(self.rs_map.set_marker(
                t["lat"], t["lon"], text=f"{serial} {t.get('alt', 0) / 1000:.1f} km" + (" ↓" if down else " ↑"),
                text_color="#b45309" if down else "#0369a1", marker_color_circle="#1e293b", marker_color_outside=col))
