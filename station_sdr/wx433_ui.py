"""Carte « Station météo locale (433 MHz) » de la page Météo : choix du
capteur reçu par rtl_433, affichage des valeurs et publication APRS-WX
(réseau CWOP / aprs.fi) par le service de surveillance du Pi."""

import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

import requests

from . import theme


def _f(v, fmt="{:.1f}"):
    return fmt.format(v).replace(".", ",")


def _ago(t):
    if not t:
        return "jamais"
    d = time.time() - t
    if d < 90:
        return "à l'instant"
    if d < 3600:
        return f"il y a {d / 60:.0f} min"
    if d < 86400 * 2:
        return f"il y a {d / 3600:.0f} h"
    return f"il y a {d / 86400:.0f} j"


def wx_line(vals: dict) -> str:
    parts = []
    if "temp_c" in vals:
        parts.append(f"{_f(vals['temp_c'])} °C")
    if "humidity" in vals:
        parts.append(f"{vals['humidity']:.0f} %")
    if "pressure_hpa" in vals:
        parts.append(f"{_f(vals['pressure_hpa'])} hPa")
    if "wind_kmh" in vals:
        w = f"vent {_f(vals['wind_kmh'], '{:.0f}')} km/h"
        if "wind_dir" in vals:
            w = f"vent {vals['wind_dir']:.0f}° {_f(vals['wind_kmh'], '{:.0f}')} km/h"
        if "gust_kmh" in vals:
            w += f" (rafales {_f(vals['gust_kmh'], '{:.0f}')})"
        parts.append(w)
    if "rain_mm" in vals:
        parts.append(f"pluie cumulée {_f(vals['rain_mm'])} mm")
    return " · ".join(parts) or "—"


class Wx433Mixin:
    def _build_wx433_card(self, parent):
        c = theme.card(parent)
        c.pack(fill="x", pady=(8, 0))
        f = c.inner
        f.configure(padding=12)
        top = tk.Frame(f, bg=theme.BG_CARD)
        top.pack(fill="x")
        tk.Label(top, text="Station météo locale (433 MHz)", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_SECTION).pack(side="left")
        self.wx4_age = tk.Label(top, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.wx4_age.pack(side="left", padx=8)
        self.wx4_values = tk.Label(top, text="Aucun capteur choisi", bg=theme.BG_CARD, fg=theme.TEXT,
                                   font=theme.FONT_BODY_BOLD, justify="left")
        self.wx4_values.pack(side="left", padx=(12, 0))
        r = tk.Frame(f, bg=theme.BG_CARD)
        r.pack(fill="x", pady=(6, 0))
        tk.Label(r, text="Capteur :", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE).pack(side="left")
        self.wx4_dev = tk.StringVar()
        self.wx4_combo = ttk.Combobox(r, textvariable=self.wx4_dev, state="readonly", width=44)
        self.wx4_combo.pack(side="left", padx=6)
        r2 = r
        self.wx4_pub = tk.BooleanVar(value=False)
        self.wx4_pub_cb = tk.Checkbutton(
            r2, text="Publier sur APRS-IS (CWOP, aprs.fi)", variable=self.wx4_pub, bg=theme.BG_CARD, fg=theme.TEXT,
            selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG_CARD, activeforeground=theme.TEXT,
            font=theme.FONT_BODY, highlightthickness=0)
        self.wx4_pub_cb.pack(side="left", padx=(12, 0))
        ttk.Button(r2, text="Appliquer", style="Flat.TButton", command=self._wx4_apply).pack(side="left", padx=6)
        ttk.Button(r2, text="aprs.fi", style="Flat.TButton",
                   command=lambda: webbrowser.open(f"https://aprs.fi/#!call={getattr(self, '_wx4_call', 'F4JCF-13')}")
                   ).pack(side="left")
        self.wx4_status = tk.Label(f, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", 8),
                                   wraplength=theme.px(1100), justify="left")
        self.wx4_status.pack(anchor="w", pady=(6, 0))
        self._wx4_devices = []
        self._wx4_loaded = False
        self.after(3000, self._wx4_poll)

    def _wx4_poll(self):
        threading.Thread(target=self._wx4_fetch, daemon=True).start()
        self.after(60000 if getattr(self, "_current_view", None) == "weather" else 180000, self._wx4_poll)

    def _wx4_fetch(self):
        host, port = self._monitor_endpoint()
        try:
            r = requests.get(f"http://{host}:{port}/api/wx/status", timeout=6)
            if r.status_code == 404:
                st, err = None, "old"
            else:
                r.raise_for_status()
                st, err = r.json(), None
        except Exception as exc:
            st, err = None, str(exc)
        self.after(0, self._wx4_render, st, err)

    def _wx4_label(self, d):
        ch = f" canal {d['channel']}" if d.get("channel") not in (None, "") else ""
        v = d.get("values") or {}
        t = f", {_f(v['temp_c'])} °C" if "temp_c" in v else ""
        return f"{d.get('model')} n°{d.get('id')}{ch}{t} — {_ago(d.get('last_seen'))}"

    def _wx4_render(self, st, err):
        if not hasattr(self, "wx4_values"):
            return
        if err:
            self.wx4_status.config(
                text="Mettez à jour le Pi pour utiliser la station météo locale." if err == "old"
                else "Pi injoignable.", fg=theme.MUTED)
            return
        self._wx4_call = st.get("call") or "F4JCF-13"
        self.wx4_pub_cb.config(text=f"Publier sur APRS-IS (CWOP, aprs.fi) comme {self._wx4_call}")
        self._wx4_devices = st.get("devices") or []
        cfg = st.get("config") or {}
        labels = [self._wx4_label(d) for d in self._wx4_devices]
        self.wx4_combo.config(values=labels)
        if not self._wx4_loaded:
            self._wx4_loaded = True
            self.wx4_pub.set(bool(cfg.get("enabled")))
        if not self.wx4_dev.get() or self.wx4_dev.get() not in labels:
            idx = next((i for i, d in enumerate(self._wx4_devices) if d["key"] == cfg.get("device")), None)
            self.wx4_dev.set(labels[idx] if idx is not None else "")
        sel = st.get("selected")
        if sel:
            self.wx4_values.config(text=wx_line(sel.get("values") or {}))
            old = time.time() - (sel.get("last_seen") or 0) > 1800
            self.wx4_age.config(text=f"relevé {_ago(sel.get('last_seen'))}", fg=theme.ORANGE if old else theme.MUTED)
            if "temp_c" in (sel.get("values") or {}) and not old:
                self._wx4_garden = f"{_f(sel['values']['temp_c'])} °C"
                if "weather" in self.tile_chips:
                    cur = self.tile_chips["weather"].text.split("  ·  jardin")[0]
                    self.tile_chips["weather"].config(text=f"{cur}  ·  jardin {self._wx4_garden}")
            else:
                self._wx4_garden = None
        elif self._wx4_devices:
            self.wx4_values.config(text=f"{len(self._wx4_devices)} capteur(s) météo reçu(s) : choisissez le vôtre.")
            self.wx4_age.config(text="")
        else:
            self.wx4_values.config(text="Aucun capteur météo reçu pour l'instant.")
        msg = []
        if cfg.get("enabled"):
            msg.append(f"Dernier envoi APRS : {_ago(st.get('last_sent'))}")
            if st.get("last_error"):
                msg.append(f"⚠ {st['last_error']}")
        msg.append("Les capteurs 433 MHz ne sont reçus que pendant le tour « Capteurs 433 » de la clé SDR ; "
                   "l'envoi APRS (toutes les 10 min) s'arrête si les valeurs ont plus de 30 min.")
        self.wx4_status.config(text="\n".join(msg), fg=theme.MUTED)

    def _wx4_apply(self):
        labels = list(self.wx4_combo.cget("values") or ())
        cur = self.wx4_dev.get()
        dev = self._wx4_devices[labels.index(cur)]["key"] if cur in labels else ""
        if self.wx4_pub.get() and not dev:
            messagebox.showwarning("Station météo", "Choisissez d'abord le capteur de votre station.")
            return
        if self.wx4_pub.get() and not messagebox.askyesno(
                "Station météo",
                f"Les relevés de ce capteur seront publiés sur Internet (APRS-IS, CWOP, aprs.fi) sous "
                f"l'indicatif {getattr(self, '_wx4_call', 'F4JCF-13')}, avec la position de la station.\n\n"
                "Vérifiez que le capteur est bien le vôtre et qu'il est placé correctement (abri, à l'ombre, "
                "1,5 à 2 m du sol). Continuer ?"):
            return
        host, port = self._monitor_endpoint()
        payload = {"wx": {"enabled": bool(self.wx4_pub.get()), "device": dev}, "send_now": True}

        def worker():
            try:
                r = requests.post(f"http://{host}:{port}/api/wx/config", json=payload, timeout=8)
                r.raise_for_status()
                err = None
            except Exception as exc:
                err = str(exc)
            if err:
                self.after(0, lambda: messagebox.showwarning("Station météo", f"Réglage refusé : {err}"))
            self._wx4_fetch()

        threading.Thread(target=worker, daemon=True).start()
