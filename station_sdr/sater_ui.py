"""Pages « 121,5 MHz » et « Goniométrie » de la vignette Balises de détresse.

- 121,5 MHz : veille de la sirène des balises (service elt121 sur le Pi) :
  état, niveau au-dessus du bruit, spectrogramme audio, historique du
  niveau, détections avec enregistrement à réécouter.
- Goniométrie : saisie des relèvements des équipes, tracé sur la carte et
  estimation de la zone d'émission (exercices et recherches type SATER).
"""

import os
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from . import api_client, gonio, theme
from .config import save_config

STATION = (43.590541, 7.002072)   # F4JCF, JN33LP
TEAM_COLORS = ["#2563eb", "#16a34a", "#9333ea", "#0891b2", "#ca8a04", "#db2777", "#4b5563", "#ea580c"]
BANDS = (("406", "406 MHz · décodage COSPAS-SARSAT"), ("121", "121,5 MHz · sirène (radioralliement)"),
         ("gonio", "Goniométrie · zone probable"))
BAND_MODE = {"406": "beacon", "121": "elt121"}


def _palette():
    stops = [(0, (8, 10, 30)), (70, (20, 40, 140)), (130, (0, 170, 200)), (190, (250, 220, 40)), (255, (255, 60, 30))]
    pal = []
    for i in range(256):
        for (a, ca), (b, cb) in zip(stops, stops[1:]):
            if a <= i <= b:
                f = (i - a) / (b - a)
                pal += [int(ca[k] + (cb[k] - ca[k]) * f) for k in range(3)]
                break
    return pal


PALETTE = _palette()


def fmt_dm(lat, lon):
    def one(v, pos, neg):
        h = pos if v >= 0 else neg
        v = abs(v)
        d = int(v)
        return f"{d}°{(v - d) * 60:06.3f}' {h}".replace(".", ",")
    return f"{one(lat, 'N', 'S')}  {one(lon, 'E', 'O')}"


class SaterMixin:
    # ------------------------------------------------ Barre des bandes --

    def _build_beacon_tab(self, frame):
        bar = tk.Frame(frame, bg=theme.BG)
        bar.pack(fill="x", padx=16, pady=(8, 0))
        self._band_btns = {}
        for key, txt in BANDS:
            b = tk.Button(bar, text=txt, font=theme.FONT_BODY_BOLD, relief="flat", bd=0, padx=14, pady=6,
                          cursor="hand2", command=lambda k=key: self._beacon_band_select(k, switch=True))
            b.pack(side="left", padx=(0, 6))
            self._band_btns[key] = b
        body = tk.Frame(frame, bg=theme.BG)
        body.pack(fill="both", expand=True)
        self._band_frames = {k: tk.Frame(body, bg=theme.BG) for k, _ in BANDS}
        self._build_b406_panel(self._band_frames["406"])
        self._build_elt_panel(self._band_frames["121"])
        self._build_gonio_panel(self._band_frames["gonio"])
        band = self.config_data.get("beacon_band", "406")
        self._beacon_band_select(band if band in self._band_frames else "406", switch=False)

    def _beacon_band_select(self, key, switch=False):
        self._beacon_band = key
        for k, f in self._band_frames.items():
            f.pack_forget()
        self._band_frames[key].pack(fill="both", expand=True)
        for k, b in self._band_btns.items():
            on = k == key
            b.config(bg=theme.ACCENT if on else theme.BG_CARD_ALT, fg="#1a1200" if on else theme.TEXT,
                     activebackground=theme.ACCENT, activeforeground="#1a1200")
        if self.config_data.get("beacon_band") != key:
            self.config_data["beacon_band"] = key
            try:
                save_config(self.config_data)
            except Exception:
                pass
        if switch and BAND_MODE.get(key):
            self._switch_sdr(BAND_MODE[key])
        if key in ("121", "gonio"):
            self._elt_start_poll()
        if key == "gonio":
            self.after(100, self._gonio_redraw)

    def _beacon_page_mode(self):
        return BAND_MODE.get(getattr(self, "_beacon_band", "406"))

    # ------------------------------------------------------ 121,5 MHz --

    def _elt_endpoint(self):
        return self.config_data.get("pi_host", "192.168.1.229"), self.config_data.get("port_elt121", 5011)

    def _build_elt_panel(self, frame):
        outer = tk.Frame(frame, bg=theme.BG)
        outer.pack(fill="both", expand=True, padx=4, pady=4)
        left = tk.Frame(outer, bg=theme.BG, width=800)
        left.pack(side="left", fill="y", pady=(0, 6))
        left.pack_propagate(False)

        c = theme.card(left)
        c.pack(fill="x", padx=12, pady=(8, 6))
        st = c.inner
        st.configure(padding=12)
        row = tk.Frame(st, bg=theme.BG_CARD)
        row.pack(fill="x")
        tk.Label(row, text="Veille 121,5 MHz", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_SECTION).pack(side="left")
        self.elt_pill = theme.pill(row, "INCONNU", theme.MUTED)
        self.elt_pill.pack(side="left", padx=(12, 0))
        ttk.Button(row, text="Effacer", style="Flat.TButton",
                   command=self._elt_clear).pack(side="right")
        ttk.Button(row, text="Écouter le 121,5 MHz", style="Accent.TButton",
                   command=lambda: self._switch_sdr("elt121")).pack(side="right", padx=(0, 8))
        cf = tk.Frame(st, bg=theme.BG_CARD)
        cf.pack(fill="x", pady=(8, 0))
        tk.Label(cf, text="Fréquence (MHz) :", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE).pack(side="left")
        self.elt_freq_var = tk.StringVar(value="121,500")
        ttk.Combobox(cf, textvariable=self.elt_freq_var, width=9,
                     values=("121,500", "243,000", "121,375", "121,775")).pack(side="left", padx=(4, 12))
        tk.Label(cf, text="Gain (dB) :", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE).pack(side="left")
        self.elt_gain_var = tk.StringVar(value="40")
        ttk.Combobox(cf, textvariable=self.elt_gain_var, width=5,
                     values=("20", "30", "35", "40", "45", "49.6")).pack(side="left", padx=(4, 12))
        ttk.Button(cf, text="Appliquer", style="Flat.TButton", command=self._elt_apply).pack(side="left")
        self.elt_info = tk.Label(st, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE,
                                 justify="left", anchor="w", wraplength=760)
        self.elt_info.pack(anchor="w", pady=(6, 0))

        # Grand état + niveau
        big = theme.card(left)
        big.pack(fill="x", padx=12, pady=(0, 6))
        self.elt_state = tk.Label(big.inner, text="—", bg=theme.BG_CARD_ALT, fg=theme.TEXT,
                                  font=("Segoe UI", 16, "bold"), pady=6)
        self.elt_state.pack(fill="x")
        self.elt_meter = tk.Canvas(big.inner, height=58, bg=theme.BG_CARD, highlightthickness=0)
        self.elt_meter.pack(fill="x", padx=8, pady=6)

        # Spectrogramme audio
        tk.Label(left, text="Spectrogramme audio (6 dernières secondes) — la sirène dessine des « dents de scie »",
                 bg=theme.BG, fg=theme.TEXT, font=theme.FONT_BODY_BOLD).pack(anchor="w", padx=12, pady=(2, 2))
        wf = theme.card(left)
        wf.pack(fill="x", padx=12, pady=(0, 4))
        self.elt_wf = tk.Canvas(wf.inner, height=160, bg="#05070f", highlightthickness=0)
        self.elt_wf.pack(fill="x", padx=1, pady=1)
        self._elt_wf_img = None
        pb = tk.Frame(left, bg=theme.BG)
        pb.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Button(pb, text="▶ Écouter les 10 dernières secondes", style="Accent.TButton",
                   command=lambda: self._elt_play(None)).pack(side="left")
        ttk.Button(pb, text="■ Stop", style="Flat.TButton", command=self._elt_stop_sound).pack(side="left", padx=6)
        ttk.Button(pb, text="Enregistrer 30 s (WAV)…", style="Flat.TButton",
                   command=lambda: self._elt_save(None)).pack(side="left")

        tk.Label(left, text="Détections (double-clic : réécouter)", bg=theme.BG, fg=theme.TEXT,
                 font=theme.FONT_SECTION).pack(anchor="w", padx=12, pady=(2, 4))
        tb = theme.card(left)
        tb.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        cols = ("start", "end", "dur", "lvl", "freq", "sweep")
        self.elt_tree = ttk.Treeview(tb.inner, columns=cols, show="headings", height=5)
        for cname, (t, w) in {"start": ("Début", 130), "end": ("Fin", 90), "dur": ("Durée", 80),
                              "lvl": ("Niveau max", 110), "freq": ("Fréquence", 90),
                              "sweep": ("Balayage", 110)}.items():
            self.elt_tree.heading(cname, text=t)
            self.elt_tree.column(cname, width=w, anchor="center")
        self.elt_tree.tag_configure("on", background="#5a1f22", foreground="#ffffff")
        vsb = ttk.Scrollbar(tb.inner, orient="vertical", command=self.elt_tree.yview)
        self.elt_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.elt_tree.pack(fill="both", expand=True, padx=1, pady=1)
        self.elt_tree.bind("<Double-1>", lambda _e: self._elt_play_selected())
        eb = tk.Frame(left, bg=theme.BG)
        eb.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Button(eb, text="▶ Écouter l'enregistrement", style="Flat.TButton",
                   command=self._elt_play_selected).pack(side="left")
        ttk.Button(eb, text="Enregistrer le WAV…", style="Flat.TButton",
                   command=lambda: self._elt_save(self._elt_selected_rec())).pack(side="left", padx=6)

        right = tk.Frame(outer, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True, pady=(8, 12), padx=(0, 12))
        tk.Label(right, text="Niveau reçu (15 dernières minutes)", bg=theme.BG, fg=theme.TEXT,
                 font=theme.FONT_SECTION).pack(anchor="w", pady=(0, 4))
        lc = theme.card(right)
        lc.pack(fill="both", expand=True)
        self.elt_chart = tk.Canvas(lc.inner, bg=theme.BG_CARD, highlightthickness=0)
        self.elt_chart.pack(fill="both", expand=True, padx=1, pady=1)
        self.elt_chart.bind("<Configure>", lambda _e: self._elt_draw_chart())
        tk.Label(right, text=(
            "Le 121,5 MHz ne transporte ni identifiant ni position : c'est une porteuse AM modulée par une "
            "tonalité qui descend de ~1600 à ~300 Hz, 2 à 4 fois par seconde (radioralliement des ELT, PLB, "
            "EPIRB). La station signale qu'une balise émet et mesure son niveau ; la localisation se fait au "
            "goniomètre (onglet « Goniométrie »).\n"
            "Le niveau dépend du gain choisi : le garder fixe pour comparer. Une montée lente du niveau peut "
            "venir d'un avion qui survole avec la balise, ou de la propagation.\n"
            "Un aéronef en difficulté peut aussi parler sur 121,5 (fréquence de détresse aviation) : écouter "
            "les enregistrements. En cas de détection réelle hors exercice : prévenir l'ARCC Lyon "
            "(via le 112 / 196 en mer)."),
            bg=theme.BG, fg=theme.MUTED, font=("Segoe UI", 9), justify="left", wraplength=900,
        ).pack(anchor="w", pady=(8, 0))

        self._elt_status = None
        self._elt_levels = []
        self._elt_events = []
        self._elt_poll_running = False
        self._elt_tick = 0
        self._elt_playing = False

    def _elt_start_poll(self):
        if not getattr(self, "_elt_poll_running", False):
            self._elt_poll_running = True
            self._elt_poll()

    def _elt_poll(self):
        if getattr(self, "_current_view", None) != "beacon" or getattr(self, "_beacon_band", "") not in ("121", "gonio"):
            self._elt_poll_running = False
            return
        full = self._beacon_band == "121"
        tick = self._elt_tick
        self._elt_tick += 1
        threading.Thread(target=self._elt_fetch, args=(full, full and tick % 3 == 0), daemon=True).start()
        self.after(2000, self._elt_poll)

    def _elt_fetch(self, full=True, slow=True):
        host, port = self._elt_endpoint()
        data = {}
        try:
            data["status"] = api_client.get_elt(host, port, "status")
            if full:
                data["wf"] = api_client.get_elt(host, port, "waterfall")
            if slow:
                data["level"] = api_client.get_elt(host, port, "level", seconds=900)
                data["events"] = api_client.get_elt(host, port, "events")
        except Exception as exc:
            self.after(0, self._elt_render, None, str(exc))
            return
        self.after(0, self._elt_render, data, None)

    def _elt_fetch_events_bg(self):
        """Appelé par le rafraîchissement général : alerte même hors de la page."""
        if getattr(self, "_sdr_mode", None) != "elt121":
            return
        host, port = self._elt_endpoint()
        try:
            st = api_client.get_elt(host, port, "status")
            ev = api_client.get_elt(host, port, "events")
        except Exception:
            return
        self.after(0, self._elt_check_alerts, ev, st)

    def _elt_check_alerts(self, events, status=None):
        for e in (events or [])[:3]:
            if time.time() - (e.get("start") or 0) > 600:
                continue
            self._raise_alert(
                f"elt:{int(e['start'])}", "Sirène de balise sur 121,5 MHz",
                f"Détectée à {time.strftime('%H:%M:%S', time.localtime(e['start']))} sur {e.get('freq_mhz')} MHz\n"
                f"Niveau : +{e.get('max_above_db')} dB au-dessus du bruit · {e.get('sweeps_per_s')} balayages/s "
                f"({e.get('direction') or '?'})\nRéécouter : Balises de détresse → 121,5 MHz.", cooldown=900)
        if status is not None and "beacon" in self.tile_chips:
            self.tile_chips["beacon"].config(
                text=("⚠ SIRÈNE 121,5" if status.get("active") else
                      f"121,5 : veille · {status.get('events_today', 0)} détection(s)"))

    def _elt_render(self, data, error):
        if not hasattr(self, "elt_pill"):
            return
        mode = getattr(self, "_sdr_mode", None)
        if error:
            sw = getattr(self, "_sdr_switch_error", None)
            if sw and sw[0] == "elt121":
                self.elt_pill.config(text="  BASCULE IMPOSSIBLE  ", bg=theme.RED)
                self.elt_info.config(text=f"La clé n'a pas pu passer sur le 121,5 MHz : {sw[1]}", fg=theme.ORANGE)
            elif mode not in (None, "elt121"):
                self.elt_pill.config(text="  EN PAUSE  " if mode != "off" else "  CLÉ ARRÊTÉE  ", bg=theme.MUTED)
                self.elt_info.config(text=(f"La clé SDR est sur {getattr(self, 'SDR_LABELS', {}).get(mode, mode)}. Cliquez sur « Écouter le 121,5 MHz » "
                                           "pour lui donner la clé."), fg=theme.MUTED)
            else:
                self.elt_pill.config(text="  NON INSTALLÉ / INJOIGNABLE  ", bg=theme.MUTED)
                self.elt_info.config(text="Le service elt121 (port 5011) ne répond pas — lancer install_extras.sh "
                                          "sur le Pi.", fg=theme.MUTED)
            self.elt_state.config(text="—", bg=theme.BG_CARD_ALT, fg=theme.MUTED)
            self._gonio_station_line(None)
            return
        st = data.get("status") or {}
        self._elt_status = st
        if "level" in data:
            self._elt_levels = data["level"] or []
        if "events" in data:
            self._elt_events = data["events"] or []
            self._elt_check_alerts(self._elt_events)
            self._elt_fill_events()
        self._gonio_station_line(st)
        if "beacon" in self.tile_chips and mode == "elt121":
            self.tile_chips["beacon"].config(
                text="⚠ SIRÈNE 121,5" if st.get("active") else f"121,5 : veille · {st.get('events_today', 0)} détection(s)")
        if not hasattr(self, "_elt_conf_loaded"):
            self._elt_conf_loaded = True
            self.elt_freq_var.set(f"{st.get('freq_mhz', 121.5):.3f}".replace(".", ","))
            self.elt_gain_var.set(f"{st.get('gain', 40):g}")
        if st.get("running"):
            self.elt_pill.config(text="  SIRÈNE DÉTECTÉE  " if st.get("active") else "  VEILLE  ",
                                 bg=theme.RED if st.get("active") else theme.GREEN)
        else:
            self.elt_pill.config(text="  ARRÊTÉ  ", bg=theme.RED)
        err = f"\n⚠ {st['error']}" if st.get("error") else ""
        self.elt_info.config(text=(
            f"{st.get('freq_mhz', 0):.3f} MHz AM · gain {st.get('gain')} dB · ressemblance avec une sirène : "
            f"{round((st.get('score') or 0) * 100)} % (tonalité {round((st.get('tonal') or 0) * 100)} %, "
            f"écart {st.get('span_hz', 0)} Hz, {st.get('sweeps_per_s', 0)} balayage(s)/s"
            f"{', ' + st['direction'] if st.get('direction') else ''})\n"
            f"{st.get('events_today', 0)} détection(s) aujourd'hui{err}").replace(".", ",", 1),
            fg=theme.ORANGE if err else theme.MUTED)
        if st.get("active"):
            since = time.time() - (st.get("active_since") or time.time())
            self.elt_state.config(text=f"🆘  SIRÈNE DÉTECTÉE depuis {int(since // 60)} min {int(since % 60):02d} s",
                                  bg=theme.RED, fg="#0b0f17")
        else:
            self.elt_state.config(text="Veille — aucune sirène en ce moment" if st.get("running") else "Écoute arrêtée",
                                  bg=theme.BG_CARD_ALT, fg=theme.TEXT)
        self._elt_draw_meter(st)
        if data.get("wf"):
            self._elt_draw_wf(data["wf"])
        if "level" in data:
            self._elt_draw_chart()

    def _elt_draw_meter(self, st):
        c = self.elt_meter
        c.delete("all")
        w = max(c.winfo_width(), 300)
        above, lvl, floor = st.get("above_db"), st.get("level_db"), st.get("floor_db")
        x0, x1, y0, y1 = 10, w - 230, 12, 40
        c.create_rectangle(x0, y0, x1, y1, fill=theme.BG_CARD_ALT, outline=theme.BORDER)
        if above is not None:
            frac = max(0.0, min(1.0, above / 40))
            col = theme.GREEN if above < 6 else theme.ORANGE if above < 15 else theme.RED
            c.create_rectangle(x0, y0, x0 + (x1 - x0) * frac, y1, fill=col, outline="")
        for db in (0, 10, 20, 30, 40):
            x = x0 + (x1 - x0) * db / 40
            c.create_line(x, y1, x, y1 + 4, fill=theme.MUTED)
            c.create_text(x, y1 + 11, text=f"+{db}", fill=theme.MUTED, font=("Segoe UI", 7))
        c.create_text(x1 + 12, 20, anchor="w", fill=theme.TEXT, font=("Segoe UI", 16, "bold"),
                      text=f"+{above:.1f} dB".replace(".", ",") if above is not None else "— dB")
        c.create_text(x1 + 12, 42, anchor="w", fill=theme.MUTED, font=("Segoe UI", 8),
                      text=(f"au-dessus du bruit · bruit {floor:.1f} · brut {lvl:.1f}".replace(".", ",")
                            if lvl is not None and floor is not None else "au-dessus du bruit de fond"))

    def _elt_draw_wf(self, wf):
        rows = wf.get("rows") or []
        c = self.elt_wf
        w, h = max(c.winfo_width(), 200), max(c.winfo_height(), 100)
        c.delete("all")
        if not rows:
            return
        flat = sorted(v for r in rows for v in r)
        lo = flat[int(len(flat) * 0.2)]
        hi = max(lo + 20, flat[min(len(flat) - 1, int(len(flat) * 0.995))])
        nb = len(rows[0])
        img = Image.new("P", (len(rows), nb))
        img.putpalette(PALETTE)
        px = []
        for j in range(nb - 1, -1, -1):        # basses fréquences en bas
            for r in rows:
                v = r[j] if j < len(r) else lo
                px.append(int(max(0, min(255, (v - lo) * 255 / (hi - lo)))))
        img.putdata(px)
        lm = 44
        img = img.convert("RGB").resize((w - lm, h - 16), Image.BILINEAR)
        self._elt_wf_img = ImageTk.PhotoImage(img)
        c.create_image(lm, 0, anchor="nw", image=self._elt_wf_img)
        f_lo, f_hi = wf.get("f_lo", 94), wf.get("f_hi", 2906)
        for f in (500, 1000, 1500, 2000, 2500):
            y = (h - 16) * (1 - (f - f_lo) / (f_hi - f_lo))
            c.create_line(lm - 4, y, lm, y, fill=theme.MUTED)
            c.create_text(lm - 6, y, text=f"{f}", anchor="e", fill=theme.MUTED, font=("Segoe UI", 7))
        c.create_text(4, 4, text="Hz", anchor="nw", fill=theme.MUTED, font=("Segoe UI", 7))
        span = len(rows) * wf.get("dt", 0.043)
        for s in range(0, int(span) + 1):
            x = lm + (w - lm) * (1 - s / span) if span else w
            c.create_text(min(x, w - 14), h - 8, text="maintenant" if s == 0 else f"−{s} s",
                          fill=theme.MUTED, font=("Segoe UI", 7), anchor="e" if s == 0 else "center")

    def _elt_draw_chart(self):
        c = getattr(self, "elt_chart", None)
        if c is None:
            return
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50 or h < 50:
            return
        pts = self._elt_levels
        ml, mr, mt, mb = 46, 12, 12, 26
        if len(pts) < 2:
            c.create_text(w / 2, h / 2, text="Pas encore de mesures (la clé doit être sur le 121,5 MHz).",
                          fill=theme.MUTED, font=theme.FONT_BODY)
            return
        now = time.time()
        t0 = now - 900
        vals = [v for _t, v in pts]
        st = self._elt_status or {}
        floor = st.get("floor_db")
        vmin = min(vals + ([floor] if floor is not None else [])) - 2
        vmax = max(max(vals) + 2, vmin + 15)

        def X(t):
            return ml + (w - ml - mr) * (t - t0) / 900

        def Y(v):
            return mt + (h - mt - mb) * (1 - (v - vmin) / (vmax - vmin))

        for e in self._elt_events:
            s, en = e.get("start") or 0, e.get("end") or now
            if en >= t0:
                c.create_rectangle(X(max(s, t0)), mt, X(en), h - mb, fill="#4a1d1f", outline="")
        step = 5 if vmax - vmin < 30 else 10
        v = int(vmin // step + 1) * step
        while v < vmax:
            c.create_line(ml, Y(v), w - mr, Y(v), fill=theme.BORDER)
            c.create_text(ml - 6, Y(v), text=f"{v}", anchor="e", fill=theme.MUTED, font=("Segoe UI", 8))
            v += step
        c.create_text(6, mt, text="dB", anchor="nw", fill=theme.MUTED, font=("Segoe UI", 8))
        for m in (15, 10, 5, 0):
            x = X(now - m * 60)
            c.create_line(x, h - mb, x, h - mb + 4, fill=theme.MUTED)
            c.create_text(x, h - mb + 13, text="maintenant" if m == 0 else f"−{m} min", fill=theme.MUTED,
                          font=("Segoe UI", 8), anchor="e" if m == 0 else "center")
        if floor is not None:
            c.create_line(ml, Y(floor), w - mr, Y(floor), fill=theme.MUTED, dash=(4, 3))
            c.create_text(w - mr - 4, Y(floor) - 8, text="bruit de fond", anchor="e", fill=theme.MUTED,
                          font=("Segoe UI", 8))
        line = []
        for t, v in pts:
            if t >= t0:
                line += [X(t), Y(v)]
        if len(line) >= 4:
            c.create_line(*line, fill=theme.ACCENT, width=2)

    def _elt_fill_events(self):
        tree = self.elt_tree
        sel = tree.selection()
        tree.delete(*tree.get_children())
        for i, e in enumerate(self._elt_events[:100]):
            s, en = e.get("start") or 0, e.get("end")
            dur = (en or time.time()) - s
            tree.insert("", "end", iid=str(i), tags=("on",) if en is None else (), values=(
                time.strftime("%d/%m %H:%M:%S", time.localtime(s)),
                "en cours" if en is None else time.strftime("%H:%M:%S", time.localtime(en)),
                f"{int(dur // 60)} min {int(dur % 60):02d} s",
                f"+{e.get('max_above_db')} dB".replace(".", ","),
                f"{e.get('freq_mhz', 0):.3f}".replace(".", ","),
                f"{e.get('sweeps_per_s')}/s {'↓' if e.get('direction') == 'descendant' else '↑'}"))
        for s in sel:
            if tree.exists(s):
                tree.selection_set(s)

    def _elt_selected_rec(self):
        sel = self.elt_tree.selection()
        if not sel:
            messagebox.showinfo("121,5 MHz", "Sélectionnez d'abord une détection dans la liste.")
            return None
        i = int(sel[0])
        return self._elt_events[i].get("rec") if i < len(self._elt_events) else None

    def _elt_play_selected(self):
        rec = self._elt_selected_rec()
        if rec:
            self._elt_play(rec)

    def _elt_get_wav(self, rec, seconds, done):
        host, port = self._elt_endpoint()

        def worker():
            try:
                data = api_client.get_elt_wav(host, port, rec=rec, seconds=seconds)
                err = None
            except Exception as exc:
                data, err = None, str(exc)
            self.after(0, done, data, err)

        threading.Thread(target=worker, daemon=True).start()

    def _elt_play(self, rec):
        def done(data, err):
            if err:
                messagebox.showwarning("121,5 MHz", f"Audio indisponible : {err}")
                return

            def play():
                try:
                    import winsound
                    winsound.PlaySound(data, winsound.SND_MEMORY)
                except Exception:
                    try:
                        path = os.path.join(tempfile.gettempdir(), "station_sdr_121.wav")
                        with open(path, "wb") as f:
                            f.write(data)
                        os.startfile(path)  # type: ignore[attr-defined]
                    except Exception:
                        pass
            threading.Thread(target=play, daemon=True).start()

        self._elt_get_wav(rec, 10, done)

    def _elt_stop_sound(self):
        try:
            import winsound
            winsound.PlaySound(None, 0)
        except Exception:
            pass

    def _elt_save(self, rec):
        def done(data, err):
            if err:
                messagebox.showwarning("121,5 MHz", f"Audio indisponible : {err}")
                return
            name = rec or time.strftime("121-5_%Y%m%d-%H%M%S.wav")
            path = filedialog.asksaveasfilename(title="Enregistrer l'audio", defaultextension=".wav",
                                                initialfile=name, filetypes=[("Audio WAV", "*.wav")])
            if path:
                with open(path, "wb") as f:
                    f.write(data)

        self._elt_get_wav(rec, 30, done)

    def _elt_apply(self):
        try:
            freq = float(self.elt_freq_var.get().replace(",", ".").strip())
            gain = float(self.elt_gain_var.get().replace(",", ".").strip())
        except ValueError:
            messagebox.showwarning("121,5 MHz", "Fréquence ou gain invalide.")
            return
        host, port = self._elt_endpoint()

        def worker():
            try:
                api_client.set_elt_config(host, port, freq_mhz=freq, gain=gain)
                err = None
            except Exception as exc:
                err = str(exc)
            self.after(0, lambda: messagebox.showwarning("121,5 MHz", f"Réglage refusé : {err}") if err else None)

        threading.Thread(target=worker, daemon=True).start()

    def _elt_clear(self):
        if not messagebox.askyesno("121,5 MHz", "Effacer la liste des détections ?"):
            return
        host, port = self._elt_endpoint()

        def worker():
            try:
                api_client.elt_clear(host, port)
            except Exception:
                pass
            self._elt_fetch(True, True)

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------- Goniométrie --

    def _build_gonio_panel(self, frame):
        outer = tk.Frame(frame, bg=theme.BG)
        outer.pack(fill="both", expand=True, padx=4, pady=4)
        left = tk.Frame(outer, bg=theme.BG, width=800)
        left.pack(side="left", fill="y", pady=(0, 6))
        left.pack_propagate(False)

        c = theme.card(left)
        c.pack(fill="x", padx=12, pady=(8, 6))
        f = c.inner
        f.configure(padding=12)
        tk.Label(f, text="Relèvements des équipes", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_SECTION).pack(anchor="w")
        r1 = tk.Frame(f, bg=theme.BG_CARD)
        r1.pack(fill="x", pady=(8, 2))
        tk.Label(r1, text="Équipe :", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY).pack(side="left")
        self.g_team = tk.StringVar(value="Équipe 1")
        ttk.Entry(r1, textvariable=self.g_team, width=14).pack(side="left", padx=(4, 12))
        tk.Label(r1, text="Position :", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY).pack(side="left")
        self.g_pos = tk.StringVar()
        ttk.Entry(r1, textvariable=self.g_pos, width=24).pack(side="left", padx=4)
        self.g_click_btn = ttk.Button(r1, text="📍 Sur la carte", style="Flat.TButton", command=self._gonio_click_mode)
        self.g_click_btn.pack(side="left", padx=4)
        ttk.Button(r1, text="Ma station", style="Flat.TButton",
                   command=lambda: self.g_pos.set(f"{STATION[0]:.5f}, {STATION[1]:.5f}")).pack(side="left")
        r2 = tk.Frame(f, bg=theme.BG_CARD)
        r2.pack(fill="x", pady=4)
        tk.Label(r2, text="Azimut (°) :", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY).pack(side="left")
        self.g_az = tk.StringVar()
        az_entry = ttk.Entry(r2, textvariable=self.g_az, width=6)
        az_entry.pack(side="left", padx=(4, 12))
        az_entry.bind("<Return>", lambda _e: self._gonio_add())
        self.g_mag = tk.BooleanVar(value=self.config_data.get("gonio_magnetic", True))
        tk.Checkbutton(r2, text="lu au compas (magnétique), déclinaison", variable=self.g_mag,
                       bg=theme.BG_CARD, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG_CARD,
                       activeforeground=theme.TEXT, font=theme.FONT_BODY, highlightthickness=0,
                       command=self._gonio_save).pack(side="left")
        self.g_decl = tk.StringVar(value=str(self.config_data.get("gonio_declination", 3.0)).replace(".", ","))
        ttk.Entry(r2, textvariable=self.g_decl, width=4).pack(side="left", padx=4)
        tk.Label(r2, text="° E", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY).pack(side="left")
        ttk.Button(r2, text="Ajouter le relèvement", style="Accent.TButton", command=self._gonio_add).pack(side="right")
        tk.Label(f, text=("Position : « 43.6512, 6.9876 », « 43°39,07' N 6°59,26' E » ou locator JN33LP ; ou clic "
                          "sur la carte. Touche Entrée dans l'azimut = ajouter."),
                 bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", 8)).pack(anchor="w")

        tk.Label(left, text="Relèvements (double-clic : reprendre dans le formulaire)", bg=theme.BG, fg=theme.TEXT,
                 font=theme.FONT_SECTION).pack(anchor="w", padx=12, pady=(2, 4))
        tb = theme.card(left)
        tb.pack(fill="x", padx=12, pady=(0, 4))
        cols = ("time", "team", "pos", "az", "azin")
        self.g_tree = ttk.Treeview(tb.inner, columns=cols, show="headings", height=5)
        for cname, (t, w, a) in {"time": ("Heure", 70, "center"), "team": ("Équipe", 130, "w"),
                                 "pos": ("Position", 270, "w"), "az": ("Azimut vrai", 100, "center"),
                                 "azin": ("Lu", 90, "center")}.items():
            self.g_tree.heading(cname, text=t)
            self.g_tree.column(cname, width=w, anchor=a)
        self.g_tree.pack(fill="x", padx=1, pady=1)
        self.g_tree.bind("<Double-1>", lambda _e: self._gonio_edit())
        bb = tk.Frame(left, bg=theme.BG)
        bb.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Button(bb, text="Supprimer la ligne", style="Flat.TButton", command=self._gonio_delete).pack(side="left")
        ttk.Button(bb, text="Tout effacer", style="Flat.TButton", command=self._gonio_clear).pack(side="left", padx=6)
        ttk.Button(bb, text="Copier le compte rendu", style="Flat.TButton", command=self._gonio_copy).pack(side="left")
        tk.Label(bb, text="Traits (km) :", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE).pack(side="left", padx=(16, 4))
        self.g_len = tk.StringVar(value=f"{float(self.config_data.get('gonio_line_km', 40)):g}")
        sp = ttk.Combobox(bb, textvariable=self.g_len, width=4, values=("10", "20", "40", "60", "100"))
        sp.pack(side="left")
        sp.bind("<<ComboboxSelected>>", lambda _e: self._gonio_redraw())

        rc = theme.card(left)
        rc.pack(fill="x", padx=12, pady=(0, 6))
        r = rc.inner
        r.configure(padding=12)
        tk.Label(r, text="Zone probable", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION).pack(anchor="w")
        self.g_result = tk.Label(r, text="Ajoutez au moins deux relèvements.", bg=theme.BG_CARD, fg=theme.MUTED,
                                 font=("Segoe UI", 13, "bold"), justify="left", anchor="w", wraplength=760)
        self.g_result.pack(anchor="w", pady=(4, 0))
        self.g_detail = tk.Label(r, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY,
                                 justify="left", anchor="w", wraplength=760)
        self.g_detail.pack(anchor="w", pady=(2, 6))
        rb = tk.Frame(r, bg=theme.BG_CARD)
        rb.pack(anchor="w")
        self.g_ign_btn = ttk.Button(rb, text="Géoportail (IGN)", style="Flat.TButton", state="disabled",
                                    command=lambda: self._gonio_open("ign"))
        self.g_ign_btn.pack(side="left")
        self.g_osm_btn = ttk.Button(rb, text="OpenStreetMap", style="Flat.TButton", state="disabled",
                                    command=lambda: self._gonio_open("osm"))
        self.g_osm_btn.pack(side="left", padx=6)
        self.g_copy_btn = ttk.Button(rb, text="Copier les coordonnées", style="Flat.TButton", state="disabled",
                                     command=self._gonio_copy_pos)
        self.g_copy_btn.pack(side="left")
        self.g_station = tk.Label(left, text="", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_BODY,
                                  justify="left", anchor="w", wraplength=770)
        self.g_station.pack(anchor="w", padx=12, pady=(2, 0))
        right = tk.Frame(outer, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True, pady=(8, 12), padx=(0, 12))
        mc = theme.card(right)
        mc.pack(fill="both", expand=True)
        self.g_map = self._make_map(mc.inner)
        self.g_map.pack(fill="both", expand=True, padx=1, pady=1)
        self.g_map.set_position(43.72, 6.98)
        self.g_map.set_zoom(10)
        self.g_map.add_left_click_map_command(self._gonio_map_click)
        tk.Label(right, text=(
            "Méthode : chaque équipe relève l'azimut du signal le plus fort (antenne directive, atténuateur "
            "quand on approche). Des relèvements pris de points bien écartés (angles de croisement > 30°) "
            "donnent une zone plus petite. Les reliefs provoquent des réflexions : se méfier d'un relèvement "
            "isolé qui ne croise pas les autres."),
            bg=theme.BG, fg=theme.MUTED, font=("Segoe UI", 8), justify="left", wraplength=1000,
        ).pack(anchor="w", pady=(6, 0))
        self.g_map.add_right_click_menu_command("Prendre cette position pour le relèvement",
                                                lambda ll: self._gonio_set_pos(ll), pass_coords=True)
        self._gonio_click = False
        self._gonio_objs = []
        self._gonio_est = None
        self._gonio_fill()

    def _gonio_station_line(self, st):
        if not hasattr(self, "g_station"):
            return
        if not st:
            self.g_station.config(text="Station F4JCF : veille 121,5 MHz non active (onglet « 121,5 MHz »).",
                                  fg=theme.MUTED)
        elif st.get("active"):
            self.g_station.config(text=(f"Station F4JCF : SIRÈNE REÇUE en ce moment sur {st.get('freq_mhz')} MHz, "
                                        f"+{st.get('above_db')} dB au-dessus du bruit."), fg=theme.RED)
        else:
            self.g_station.config(text=(f"Station F4JCF : veille {st.get('freq_mhz')} MHz, pas de sirène "
                                        f"(niveau +{st.get('above_db')} dB)."), fg=theme.MUTED)

    def _gonio_list(self):
        return self.config_data.setdefault("gonio_bearings", [])

    def _gonio_save(self):
        try:
            self.config_data["gonio_magnetic"] = bool(self.g_mag.get())
            self.config_data["gonio_declination"] = float(self.g_decl.get().replace(",", "."))
            self.config_data["gonio_line_km"] = float(self.g_len.get().replace(",", "."))
        except ValueError:
            pass
        try:
            save_config(self.config_data)
        except Exception:
            pass

    def _gonio_click_mode(self):
        self._gonio_click = not self._gonio_click
        self.g_click_btn.config(text="Cliquez sur la carte…" if self._gonio_click else "📍 Sur la carte")
        try:
            self.g_map.canvas.config(cursor="crosshair" if self._gonio_click else "")
        except Exception:
            pass

    def _gonio_map_click(self, coords):
        if self._gonio_click:
            self._gonio_set_pos(coords)
            self._gonio_click_mode()

    def _gonio_set_pos(self, coords):
        self.g_pos.set(f"{coords[0]:.5f}, {coords[1]:.5f}")

    def _gonio_add(self):
        try:
            lat, lon = gonio.parse_position(self.g_pos.get())
        except ValueError as exc:
            messagebox.showwarning("Goniométrie", f"Position non reconnue ({exc}).\nExemples : 43.6512, 6.9876 — "
                                                  "43°39,07' N 6°59,26' E — JN33LP")
            return
        try:
            az_in = float(self.g_az.get().replace(",", ".").replace("°", "").strip())
            decl = float(self.g_decl.get().replace(",", ".")) if self.g_mag.get() else 0.0
        except ValueError:
            messagebox.showwarning("Goniométrie", "Azimut ou déclinaison invalide (degrés, ex. 35).")
            return
        if not 0 <= az_in <= 360:
            messagebox.showwarning("Goniométrie", "L'azimut doit être entre 0 et 360°.")
            return
        team = self.g_team.get().strip() or "Équipe"
        self._gonio_list().append({"team": team, "lat": lat, "lon": lon, "az_in": az_in, "mag": bool(self.g_mag.get()),
                                   "az": (az_in + decl) % 360, "time": time.time()})
        self._gonio_save()
        self.g_az.set("")
        self._gonio_fill()

    def _gonio_fill(self):
        tree = self.g_tree
        tree.delete(*tree.get_children())
        for i, b in enumerate(self._gonio_list()):
            tree.insert("", "end", iid=str(i), values=(
                time.strftime("%H:%M", time.localtime(b.get("time", 0))), b.get("team"),
                f"{b['lat']:.5f}, {b['lon']:.5f}", f"{b['az']:.0f}°",
                f"{b['az_in']:.0f}° {'magn.' if b.get('mag') else 'vrai'}"))
        self._gonio_redraw()

    def _gonio_sel(self):
        sel = self.g_tree.selection()
        return int(sel[0]) if sel else None

    def _gonio_delete(self):
        i = self._gonio_sel()
        if i is not None and i < len(self._gonio_list()):
            del self._gonio_list()[i]
            self._gonio_save()
            self._gonio_fill()

    def _gonio_edit(self):
        i = self._gonio_sel()
        if i is None:
            return
        b = self._gonio_list()[i]
        self.g_team.set(b.get("team", ""))
        self.g_pos.set(f"{b['lat']:.5f}, {b['lon']:.5f}")
        self.g_az.set(f"{b['az_in']:g}")
        self.g_mag.set(bool(b.get("mag")))
        del self._gonio_list()[i]
        self._gonio_save()
        self._gonio_fill()

    def _gonio_clear(self):
        if self._gonio_list() and messagebox.askyesno("Goniométrie", "Effacer tous les relèvements ?"):
            self._gonio_list().clear()
            self._gonio_save()
            self._gonio_fill()

    def _gonio_redraw(self):
        if not hasattr(self, "g_map"):
            return
        m = self.g_map
        for o in self._gonio_objs:
            try:
                o.delete()
            except Exception:
                pass
        self._gonio_objs = []
        try:
            length = float(self.g_len.get().replace(",", "."))
        except ValueError:
            length = 40.0
        self.config_data["gonio_line_km"] = length
        bl = self._gonio_list()
        teams = {}
        self._gonio_objs.append(m.set_marker(*STATION, text="F4JCF", text_color="#16a34a",
                                             marker_color_circle="#14532d", marker_color_outside="#22c55e"))
        for b in bl:
            col = teams.setdefault(b.get("team"), TEAM_COLORS[len(teams) % len(TEAM_COLORS)])
            end = gonio.destination(b["lat"], b["lon"], b["az"], length)
            self._gonio_objs.append(m.set_path([(b["lat"], b["lon"]), end], color=col, width=3))
            self._gonio_objs.append(m.set_marker(b["lat"], b["lon"], text=f"{b.get('team')} {b['az']:.0f}°",
                                                 text_color=col, marker_color_circle="#1e293b",
                                                 marker_color_outside=col))
        est = gonio.estimate([(b["lat"], b["lon"], b["az"]) for b in bl])
        self._gonio_est = est
        if est and est.get("lat") is not None:
            for la, lo in est["crossings"]:
                self._gonio_objs.append(m.set_path(gonio.circle(la, lo, 0.12, 16), color="#f59e0b", width=2))
            self._gonio_objs.append(m.set_path(gonio.circle(est["lat"], est["lon"], est["radius_km"]),
                                               color="#ef4444", width=3))
            self._gonio_objs.append(m.set_marker(est["lat"], est["lon"], text="Zone probable", text_color="#b91c1c",
                                                 marker_color_circle="#7f1d1d", marker_color_outside="#ef4444"))
            d, brg = gonio.dist_bearing(STATION[0], STATION[1], est["lat"], est["lon"])
            radius = f"{est['radius_km']:.1f}".replace(".", ",")
            self.g_result.config(text=(f"{est['lat']:.5f}, {est['lon']:.5f}   ({fmt_dm(est['lat'], est['lon'])})\n"
                                       f"rayon ≈ {radius} km"), fg=theme.RED)
            warn = ""
            if est["behind"]:
                names = ", ".join(bl[k].get("team", "?") for k in est["behind"])
                warn = f"\n⚠ Le point estimé est DERRIÈRE : {names} — relèvement à vérifier (réflexion, erreur de lecture ?)."
            self.g_detail.config(text=(
                f"Qualité {est['quality']} · {len(bl)} relèvements · plus petit angle de croisement "
                f"{est['min_angle']:.0f}° · à {d:.1f} km au {brg:.0f}° ({self._compass(brg)}) de F4JCF{warn}"),
                fg=theme.ORANGE if warn or est["quality"] == "faible" else theme.MUTED)
            for btn in (self.g_ign_btn, self.g_osm_btn, self.g_copy_btn):
                btn.config(state="normal")
        else:
            self.g_result.config(text="Ajoutez au moins deux relèvements." if len(bl) < 2 else
                                 "Les relèvements ne se croisent pas devant les équipes.", fg=theme.MUTED)
            self.g_detail.config(text="", fg=theme.MUTED)
            for btn in (self.g_ign_btn, self.g_osm_btn, self.g_copy_btn):
                btn.config(state="disabled")
        pts = [(b["lat"], b["lon"]) for b in bl]
        if est and est.get("lat") is not None:
            pts.append((est["lat"], est["lon"]))
        if len(pts) >= 2:
            la = [p[0] for p in pts]
            lo = [p[1] for p in pts]
            pad = 0.03
            try:
                m.fit_bounding_box((max(la) + pad, min(lo) - pad), (min(la) - pad, max(lo) + pad))
            except Exception:
                pass

    def _gonio_open(self, kind):
        est = self._gonio_est
        if not est or est.get("lat") is None:
            return
        lat, lon = est["lat"], est["lon"]
        if kind == "ign":
            url = (f"https://www.geoportail.gouv.fr/carte?c={lon},{lat}&z=14"
                   "&l0=GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2::GEOPORTAIL:OGC:WMTS(1)&permalink=yes")
        else:
            url = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=14/{lat}/{lon}"
        self._open_url(url)

    def _gonio_copy_pos(self):
        est = self._gonio_est
        if est and est.get("lat") is not None:
            self.clipboard_clear()
            self.clipboard_append(f"{est['lat']:.5f}, {est['lon']:.5f}  ({fmt_dm(est['lat'], est['lon'])})")

    def _gonio_copy(self):
        bl = self._gonio_list()
        lines = [f"Goniométrie 121,5 MHz — F4JCF — {time.strftime('%d/%m/%Y %H:%M')}", "Relèvements :"]
        for b in bl:
            lines.append(f"  {time.strftime('%H:%M', time.localtime(b.get('time', 0)))}  {b.get('team')} — "
                         f"{b['lat']:.5f}, {b['lon']:.5f} ({fmt_dm(b['lat'], b['lon'])}) — azimut vrai {b['az']:.0f}°"
                         + (f" (lu {b['az_in']:.0f}° magn.)" if b.get("mag") else ""))
        est = self._gonio_est
        if est and est.get("lat") is not None:
            d, brg = gonio.dist_bearing(STATION[0], STATION[1], est["lat"], est["lon"])
            lines += [f"Zone probable : {est['lat']:.5f}, {est['lon']:.5f} ({fmt_dm(est['lat'], est['lon'])}), "
                      f"rayon ≈ {est['radius_km']:.1f} km, qualité {est['quality']}",
                      f"Soit {d:.1f} km au {brg:.0f}° de F4JCF (Mougins)",
                      f"Géoportail : https://www.geoportail.gouv.fr/carte?c={est['lon']:.5f},{est['lat']:.5f}&z=14"]
        st = getattr(self, "_elt_status", None)
        if st and st.get("running"):
            lines.append(f"Station F4JCF : {'sirène reçue' if st.get('active') else 'pas de sirène'} sur "
                         f"{st.get('freq_mhz')} MHz (+{st.get('above_db')} dB)")
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))
