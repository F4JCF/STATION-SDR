"""Page « POCSAG » : radiomessagerie reçue par la clé SDR (service pocsag
sur le Pi, rtl_fm + multimon-ng). DAPNET 439,9875 MHz par défaut."""

import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from . import api_client, theme
from .config import save_config

POCSAG_PRESETS = ((439.9875, "DAPNET 439,9875 (radioamateur)"),)


def _ftxt(f):
    return f"{float(f):.4f}".rstrip("0").rstrip(".").replace(".", ",")


class PocsagMixin:
    def _pocsag_endpoint(self):
        return self.config_data.get("pi_host", "192.168.1.229"), self.config_data.get("port_pocsag", 5012)

    def _build_pocsag_tab(self, frame):
        outer = tk.Frame(frame, bg=theme.BG)
        outer.pack(fill="both", expand=True, padx=4, pady=4)
        head = theme.card(outer)
        head.pack(fill="x", padx=12, pady=(8, 6))
        h = head.inner
        h.configure(padding=12)
        r1 = tk.Frame(h, bg=theme.BG_CARD)
        r1.pack(fill="x")
        tk.Label(r1, text="POCSAG — radiomessagerie", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_SECTION).pack(side="left")
        self.pg_pill = theme.pill(r1, "INCONNU", theme.MUTED)
        self.pg_pill.pack(side="left", padx=(12, 0))
        self.pg_info = tk.Label(r1, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.pg_info.pack(side="left", padx=(14, 0))
        ttk.Button(r1, text="Effacer", style="Flat.TButton", command=self._pocsag_clear).pack(side="right")
        ttk.Button(r1, text="Écouter", style="Accent.TButton",
                   command=lambda: self._switch_sdr("pocsag")).pack(side="right", padx=(0, 8))

        r2 = tk.Frame(h, bg=theme.BG_CARD)
        r2.pack(fill="x", pady=(10, 0))
        self.pg_preset_btns = {}
        for f, txt in POCSAG_PRESETS:
            b = tk.Button(r2, text=txt, font=theme.FONT_BODY_BOLD, relief="flat", bd=0, padx=12, pady=5,
                          cursor="hand2", bg=theme.BG_CARD_ALT, fg=theme.TEXT, activebackground=theme.ACCENT,
                          activeforeground="#1a1200", command=lambda f=f: self._pocsag_preset(f))
            b.pack(side="left", padx=(0, 10))
            self.pg_preset_btns[f] = b
        tk.Label(r2, text="Fréquence (MHz) :", bg=theme.BG_CARD, fg=theme.MUTED,
                 font=theme.FONT_SUBTITLE).pack(side="left")
        self.pg_freq = tk.StringVar(value="439,9875")
        ttk.Entry(r2, textvariable=self.pg_freq, width=10).pack(side="left", padx=(4, 12))
        tk.Label(r2, text="Gain (dB) :", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE).pack(side="left")
        self.pg_gain = tk.StringVar(value="40")
        ttk.Combobox(r2, textvariable=self.pg_gain, width=5,
                     values=("20", "30", "35", "40", "45", "49.6")).pack(side="left", padx=(4, 12))
        ttk.Button(r2, text="Appliquer", style="Flat.TButton", command=self._pocsag_apply).pack(side="left")

        r3 = tk.Frame(h, bg=theme.BG_CARD)
        r3.pack(fill="x", pady=(8, 0))
        tk.Label(r3, text="Alerter si le message contient (séparés par des virgules) :", bg=theme.BG_CARD,
                 fg=theme.MUTED, font=theme.FONT_SUBTITLE).pack(side="left")
        self.pg_watch = tk.StringVar(value="F4JCF")
        ttk.Entry(r3, textvariable=self.pg_watch, width=36).pack(side="left", padx=6)
        self.pg_hide = tk.BooleanVar(value=True)
        tk.Checkbutton(r3, text="Masquer les messages techniques (heure, rubriques DAPNET)", variable=self.pg_hide,
                       bg=theme.BG_CARD, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG_CARD,
                       activeforeground=theme.TEXT, font=theme.FONT_BODY, highlightthickness=0).pack(side="left", padx=12)
        ttk.Button(r3, text="Enregistrer", style="Flat.TButton", command=self._pocsag_save_watch).pack(side="left")

        body = tk.Frame(outer, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        left = tk.Frame(body, bg=theme.BG)
        left.pack(side="left", fill="both", expand=True)
        sr = tk.Frame(left, bg=theme.BG)
        sr.pack(fill="x", pady=(0, 4))
        tk.Label(sr, text="Rechercher (RIC ou texte) :", bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_SUBTITLE).pack(side="left")
        self.pg_search = tk.StringVar()
        self.pg_search.trace_add("write", lambda *_a: self._pocsag_fill())
        ttk.Entry(sr, textvariable=self.pg_search, width=22).pack(side="left", padx=6)
        self.pg_count = tk.Label(sr, text="", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.pg_count.pack(side="left", padx=10)
        tbl = theme.card(left)
        tbl.pack(fill="both", expand=True)
        cols = ("time", "rate", "ric", "func", "type", "text")
        self.pg_tree = ttk.Treeview(tbl.inner, columns=cols, show="headings")
        for c, (t, w, a) in {"time": ("Heure", 120, "center"), "rate": ("Bauds", 60, "center"),
                             "ric": ("RIC (adresse)", 100, "center"), "func": ("Fonc.", 50, "center"),
                             "type": ("Type", 85, "center"), "text": ("Message", 760, "w")}.items():
            self.pg_tree.heading(c, text=t)
            self.pg_tree.column(c, width=w, minwidth=40, anchor=a, stretch=(c == "text"))
        self.pg_tree.tag_configure("watch", background="#5a3a12", foreground="#ffffff")
        self.pg_tree.tag_configure("tech", foreground=theme.MUTED)
        vsb = ttk.Scrollbar(tbl.inner, orient="vertical", command=self.pg_tree.yview)
        self.pg_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.pg_tree.pack(fill="both", expand=True, padx=1, pady=1)
        self.pg_tree.bind("<<TreeviewSelect>>", self._pocsag_select)

        det = theme.card(body)
        det.pack(side="left", fill="y", padx=(10, 0))
        d = det.inner
        d.configure(padding=12)
        self.pg_title = tk.Label(d, text="Sélectionnez un message.", bg=theme.BG_CARD, fg=theme.TEXT,
                                 font=("Segoe UI", 12, "bold"), anchor="w", justify="left", wraplength=360)
        self.pg_title.pack(anchor="w")
        self.pg_text = tk.Text(d, bg=theme.BG_CARD_ALT, fg=theme.TEXT, font=("Consolas", 10), relief="flat",
                               wrap="word", width=44, height=16, padx=8, pady=6)
        self.pg_text.pack(fill="both", expand=True, pady=(8, 6))
        self.pg_text.config(state="disabled")
        ttk.Button(d, text="Copier le message", style="Flat.TButton", command=self._pocsag_copy).pack(anchor="w")
        tk.Label(outer, text=(
            "Écoute seule. DAPNET est le réseau de radiomessagerie des radioamateurs (1200 bauds, 439,9875 MHz). "
            "Les messages d'autres réseaux (services, entreprises) ne sont pas destinés au public : la loi interdit "
            "de les divulguer ou de les exploiter. Les messages restent en mémoire sur le Pi et ne sont pas enregistrés."),
            bg=theme.BG, fg=theme.MUTED, font=("Segoe UI", 8), justify="left", wraplength=1600,
        ).pack(anchor="w", padx=12, pady=(0, 8))

        self._pg_msgs = []
        self._pg_shown = []
        self._pg_conf_loaded = False
        self._pocsag_poll_running = False

    # ------------------------------------------------------------ Données --

    def _pocsag_start_poll(self):
        if not getattr(self, "_pocsag_poll_running", False):
            self._pocsag_poll_running = True
            self._pocsag_poll()

    def _pocsag_poll(self):
        if getattr(self, "_current_view", None) != "pocsag":
            self._pocsag_poll_running = False
            return
        threading.Thread(target=self._pocsag_fetch, daemon=True).start()
        self.after(4000, self._pocsag_poll)

    def _pocsag_fetch(self):
        host, port = self._pocsag_endpoint()
        try:
            data = {"status": api_client.get_pocsag(host, port, "status"),
                    "messages": api_client.get_pocsag(host, port, "messages", limit=500)}
        except Exception as exc:
            self.after(0, self._pocsag_render, None, str(exc))
            return
        self.after(0, self._pocsag_render, data, None)

    def _pocsag_fetch_bg(self):
        """Rafraîchissement général : alertes même hors de la page."""
        if getattr(self, "_sdr_mode", None) != "pocsag" or getattr(self, "_current_view", None) == "pocsag":
            return
        host, port = self._pocsag_endpoint()
        try:
            msgs = api_client.get_pocsag(host, port, "messages", limit=50)
        except Exception:
            return
        self.after(0, self._pocsag_alerts, msgs)

    def _pocsag_alerts(self, msgs):
        for m in msgs or []:
            if m.get("watch") and time.time() - (m.get("time") or 0) < 900:
                self._raise_alert(f"pocsag:{m.get('ric')}:{(m.get('text') or '')[:60]}",
                                  f"POCSAG — message pour RIC {m.get('ric')}",
                                  f"{m.get('text') or '(tonalité)'}\n"
                                  f"{time.strftime('%H:%M:%S', time.localtime(m.get('time') or 0))} · "
                                  f"{_ftxt(m.get('freq_mhz') or 0)} MHz", cooldown=3600)
        if "pocsag" in self.tile_chips:
            n = sum(1 for m in msgs or [] if time.time() - (m.get("time") or 0) < 3600)
            self.tile_chips["pocsag"].config(text=f"{n} message(s) / h")

    def _pocsag_render(self, data, error):
        if not hasattr(self, "pg_tree"):
            return
        mode = getattr(self, "_sdr_mode", None)
        labels = getattr(self, "SDR_LABELS", {}) or {}
        if error:
            sw = getattr(self, "_sdr_switch_error", None)
            if sw and sw[0] == "pocsag":
                self.pg_pill.config(text="  BASCULE IMPOSSIBLE  ", bg=theme.RED)
                self.pg_info.config(text=str(sw[1])[:120])
            elif mode not in (None, "pocsag"):
                self.pg_pill.config(text="  EN PAUSE  " if mode != "off" else "  CLÉ ARRÊTÉE  ", bg=theme.MUTED)
                self.pg_info.config(text=f"La clé SDR est sur {labels.get(mode, mode)}.")
            else:
                self.pg_pill.config(text="  NON INSTALLÉ / INJOIGNABLE  ", bg=theme.MUTED)
                self.pg_info.config(text="Service pocsag (port 5012) injoignable : lancer install_pocsag.sh sur le Pi.")
            if "pocsag" in self.tile_chips:
                self.tile_chips["pocsag"].config(text="—")
            return
        st = data.get("status") or {}
        if not self._pg_conf_loaded:
            self._pg_conf_loaded = True
            self.pg_freq.set(_ftxt(st.get("freq_mhz", 439.9875)))
            self.pg_gain.set(f"{st.get('gain', 40):g}")
            self.pg_watch.set(", ".join(st.get("watch") or []))
            self.pg_hide.set(bool(st.get("hide_tech", True)))
        fq = float(st.get("freq_mhz") or 0)
        for f, b in self.pg_preset_btns.items():
            on = abs(f - fq) < 0.00005
            b.config(bg=theme.ACCENT if on else theme.BG_CARD_ALT, fg="#1a1200" if on else theme.TEXT)
        if st.get("running"):
            self.pg_pill.config(text="  ÉCOUTE  ", bg=theme.GREEN)
        else:
            self.pg_pill.config(text="  ARRÊTÉ  ", bg=theme.RED)
        last = st.get("last_time")
        self.pg_info.config(text=(
            f"{_ftxt(fq)} MHz · {st.get('last_hour', 0)} message(s) la dernière heure · "
            f"{st.get('watched', 0)} surveillé(s)"
            + (f" · dernier à {time.strftime('%H:%M:%S', time.localtime(last))}" if last else "")
            + (f" · ⚠ {st['error']}" if st.get("error") else "")),
            fg=theme.ORANGE if st.get("error") else theme.MUTED)
        self._pg_msgs = data.get("messages") or []
        self._pocsag_alerts(self._pg_msgs)
        self._pocsag_fill()

    def _pocsag_fill(self):
        if not hasattr(self, "pg_tree"):
            return
        q = self.pg_search.get().strip().upper()
        shown = [m for m in self._pg_msgs if not q or q in f"{m.get('ric')} {m.get('text', '')}".upper()]
        sel = self.pg_tree.selection()
        sel_key = None
        if sel and int(sel[0]) < len(self._pg_shown):
            m = self._pg_shown[int(sel[0])]
            sel_key = (m.get("time"), m.get("ric"))
        self._pg_shown = shown
        tree = self.pg_tree
        tree.delete(*tree.get_children())
        for i, m in enumerate(shown[:500]):
            tags = ("watch",) if m.get("watch") else ("tech",) if m.get("tech") else ()
            tree.insert("", "end", iid=str(i), tags=tags, values=(
                time.strftime("%d/%m %H:%M:%S", time.localtime(m.get("time") or 0)), m.get("rate"), m.get("ric"),
                m.get("function"), m.get("type"), (m.get("text") or "(tonalité seule)").replace("\n", " ⏎ ")))
            if sel_key == (m.get("time"), m.get("ric")):
                tree.selection_set(str(i))
        self.pg_count.config(text=f"{len(shown)} message(s)")

    def _pocsag_select(self, _e=None):
        sel = self.pg_tree.selection()
        if not sel or int(sel[0]) >= len(self._pg_shown):
            return
        m = self._pg_shown[int(sel[0])]
        self.pg_title.config(text=(f"RIC {m.get('ric')} · fonction {m.get('function')} · {m.get('type')}\n"
                                   f"{time.strftime('%d/%m/%Y %H:%M:%S', time.localtime(m.get('time') or 0))} · "
                                   f"{_ftxt(m.get('freq_mhz') or 0)} MHz · {m.get('rate')} bauds"),
                             fg=theme.ACCENT if m.get("watch") else theme.TEXT)
        self.pg_text.config(state="normal")
        self.pg_text.delete("1.0", "end")
        self.pg_text.insert("1.0", m.get("text") or "(tonalité seule, sans texte)")
        self.pg_text.config(state="disabled")

    def _pocsag_copy(self):
        txt = self.pg_text.get("1.0", "end").strip()
        if txt:
            self.clipboard_clear()
            self.clipboard_append(txt)

    # ------------------------------------------------------- Réglages --

    def _pocsag_config(self, **conf):
        host, port = self._pocsag_endpoint()

        def worker():
            try:
                api_client.set_pocsag_config(host, port, **conf)
                err = None
            except Exception as exc:
                err = str(exc)
            if err:
                self.after(0, lambda: messagebox.showwarning("POCSAG", f"Réglage refusé : {err}"))
            else:
                self._pocsag_fetch()

        threading.Thread(target=worker, daemon=True).start()

    def _pocsag_preset(self, freq):
        self.pg_freq.set(_ftxt(freq))
        self._pocsag_apply()
        if getattr(self, "_sdr_mode", None) != "pocsag":
            self._switch_sdr("pocsag")

    def _pocsag_apply(self):
        try:
            freq = float(self.pg_freq.get().replace(",", ".").strip())
            gain = float(self.pg_gain.get().replace(",", ".").strip())
        except ValueError:
            messagebox.showwarning("POCSAG", "Fréquence ou gain invalide.")
            return
        self._pocsag_config(freq_mhz=freq, gain=gain)

    def _pocsag_save_watch(self):
        words = [w.strip() for w in self.pg_watch.get().split(",") if w.strip()]
        self.config_data["pocsag_watch"] = words
        try:
            save_config(self.config_data)
        except Exception:
            pass
        self._pocsag_config(watch=words, hide_tech=bool(self.pg_hide.get()))

    def _pocsag_clear(self):
        if not messagebox.askyesno("POCSAG", "Effacer la liste des messages ?"):
            return
        host, port = self._pocsag_endpoint()

        def worker():
            try:
                api_client.pocsag_clear(host, port)
            except Exception:
                pass
            self._pocsag_fetch()

        threading.Thread(target=worker, daemon=True).start()
