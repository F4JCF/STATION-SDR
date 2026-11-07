"""Plusieurs stations : profils (Pi, iGate LoRa, position) entre lesquels on
bascule d'un clic sur la puce « Station » du bandeau — par exemple la station
fixe de Mougins et un Pi emporté en mission."""

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import gonio, theme
from .config import save_config

HOME_PROFILE = {"name": "Mougins (fixe)", "pi_host": "192.168.1.229", "lora_igate_host": "192.168.1.154",
                "lat": 43.590541, "lon": 7.002072}


class ProfilesMixin:
    def _profiles(self):
        profs = self.config_data.get("station_profiles")
        if not profs:
            first = dict(HOME_PROFILE)
            first["pi_host"] = self.config_data.get("pi_host", first["pi_host"])
            first["lora_igate_host"] = self.config_data.get("lora_igate_host", first["lora_igate_host"])
            profs = [first]
            self.config_data["station_profiles"] = profs
            self.config_data["station_profile"] = first["name"]
        return profs

    def _profile_current(self):
        name = self.config_data.get("station_profile")
        profs = self._profiles()
        return next((p for p in profs if p["name"] == name), profs[0])

    def _station_coords(self):
        p = self._profile_current()
        try:
            return float(p.get("lat")), float(p.get("lon"))
        except (TypeError, ValueError):
            return HOME_PROFILE["lat"], HOME_PROFILE["lon"]

    def _profile_chip_text(self):
        p = self._profile_current()
        return f"Station : {p['name']} ({self.config_data.get('pi_host', '?')}) ▾"

    def _profiles_bind_chip(self):
        self.chip_host.config(text=self._profile_chip_text())
        try:
            self.chip_host.configure(cursor="hand2")
        except tk.TclError:
            pass
        self.chip_host.bind("<Button-1>", self._profiles_menu)

    def _profiles_menu(self, event=None):
        menu = tk.Menu(self, tearoff=0, bg=theme.BG_CARD, fg=theme.TEXT, activebackground=theme.ACCENT,
                       activeforeground="#0b0f17")
        cur = self._profile_current()["name"]
        for p in self._profiles():
            menu.add_command(label=("● " if p["name"] == cur else "   ") + f"{p['name']}  —  {p['pi_host']}",
                             command=lambda n=p["name"]: self._profile_switch(n))
        menu.add_separator()
        menu.add_command(label="Gérer les stations…", command=self._profiles_dialog)
        x = event.x_root if event else self.winfo_pointerx()
        y = event.y_root if event else self.winfo_pointery()
        menu.tk_popup(x, y)

    def _profile_switch(self, name):
        p = next((p for p in self._profiles() if p["name"] == name), None)
        if not p:
            return
        self.config_data["station_profile"] = p["name"]
        self.config_data["pi_host"] = p["pi_host"]
        if p.get("lora_igate_host"):
            self.config_data["lora_igate_host"] = p["lora_igate_host"]
        save_config(self.config_data)
        self.chip_host.config(text=self._profile_chip_text())
        # Oublie ce qui venait de l'autre station
        for attr in ("_mt_thumbs",):
            if isinstance(getattr(self, attr, None), dict):
                getattr(self, attr).clear()
        if hasattr(self, "_mt_conf_loaded"):
            self._mt_conf_loaded = False
        if hasattr(self, "_wx4_loaded"):
            self._wx4_loaded = False
        threading.Thread(target=self._refresh_once, daemon=True).start()
        self._mission_log("Station", f"Bascule sur la station « {p['name']} » ({p['pi_host']})")

    def _profiles_dialog(self):
        win = tk.Toplevel(self)
        win.title("Stations")
        win.configure(bg=theme.BG)
        win.transient(self)
        f = tk.Frame(win, bg=theme.BG, padx=14, pady=12)
        f.pack(fill="both", expand=True)
        cols = ("name", "pi", "lora", "pos")
        tree = ttk.Treeview(f, columns=cols, show="headings", height=6)
        for c, (t, w) in {"name": ("Nom", 170), "pi": ("Adresse du Pi", 140), "lora": ("iGate LoRa", 130),
                          "pos": ("Position", 170)}.items():
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.grid(row=0, column=0, columnspan=4, sticky="nsew")
        ev = {k: tk.StringVar() for k in ("name", "pi", "lora", "pos")}
        labels = (("name", "Nom :"), ("pi", "Adresse du Pi :"), ("lora", "iGate LoRa (facultatif) :"),
                  ("pos", "Position (lat, lon ou locator) :"))
        for i, (k, lab) in enumerate(labels, start=1):
            tk.Label(f, text=lab, bg=theme.BG, fg=theme.MUTED, font=theme.FONT_BODY).grid(
                row=i, column=0, sticky="w", pady=2)
            ttk.Entry(f, textvariable=ev[k], width=40).grid(row=i, column=1, columnspan=3, sticky="w", pady=2, padx=6)
        tk.Label(f, bg=theme.BG, fg=theme.MUTED, font=("Segoe UI", theme.fs(8)), justify="left", wraplength=theme.px(600),
                 text=("Pour un Pi emporté en mission : son adresse sur le réseau du terrain (partage de connexion du "
                       "téléphone, routeur 4G…) ou son adresse dans le VPN. La position sert au compte rendu et à la "
                       "goniométrie.")).grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 6))

        def fill():
            tree.delete(*tree.get_children())
            for p in self._profiles():
                pos = f"{p.get('lat', 0):.5f}, {p.get('lon', 0):.5f}" if p.get("lat") is not None else "—"
                tree.insert("", "end", iid=p["name"], values=(p["name"], p["pi_host"], p.get("lora_igate_host") or "",
                                                              pos))

        def on_sel(_e=None):
            sel = tree.selection()
            if not sel:
                return
            p = next(p for p in self._profiles() if p["name"] == sel[0])
            ev["name"].set(p["name"])
            ev["pi"].set(p["pi_host"])
            ev["lora"].set(p.get("lora_igate_host") or "")
            ev["pos"].set(f"{p['lat']:.5f}, {p['lon']:.5f}" if p.get("lat") is not None else "")
        tree.bind("<<TreeviewSelect>>", on_sel)

        def save():
            name, pi = ev["name"].get().strip(), ev["pi"].get().strip()
            if not name or not pi:
                messagebox.showwarning("Stations", "Nom et adresse du Pi obligatoires.", parent=win)
                return
            lat = lon = None
            if ev["pos"].get().strip():
                try:
                    pos = gonio.parse_position(ev["pos"].get())
                except ValueError:
                    pos = None
                if not pos:
                    messagebox.showwarning("Stations", "Position illisible (ex. 43.5905, 7.0021 ou JN33LP).",
                                           parent=win)
                    return
                lat, lon = pos[0], pos[1]
            profs = self._profiles()
            old = tree.selection()[0] if tree.selection() else None
            entry = {"name": name, "pi_host": pi, "lora_igate_host": ev["lora"].get().strip(), "lat": lat, "lon": lon}
            idx = next((i for i, p in enumerate(profs) if p["name"] in (old, name)), None)
            if idx is None:
                profs.append(entry)
            else:
                was_current = profs[idx]["name"] == self.config_data.get("station_profile")
                profs[idx] = entry
                if was_current:
                    self.config_data["station_profile"] = name
                    self._profile_switch(name)
            save_config(self.config_data)
            fill()
            self.chip_host.config(text=self._profile_chip_text())

        def delete():
            sel = tree.selection()
            profs = self._profiles()
            if not sel or len(profs) < 2:
                return
            if sel[0] == self._profile_current()["name"]:
                messagebox.showinfo("Stations", "Basculez d'abord sur une autre station.", parent=win)
                return
            self.config_data["station_profiles"] = [p for p in profs if p["name"] != sel[0]]
            save_config(self.config_data)
            fill()

        def use():
            sel = tree.selection()
            if sel:
                self._profile_switch(sel[0])
                fill()

        bb = tk.Frame(f, bg=theme.BG)
        bb.grid(row=6, column=0, columnspan=4, sticky="w")
        ttk.Button(bb, text="Enregistrer (ajouter / modifier)", style="Accent.TButton", command=save).pack(side="left")
        ttk.Button(bb, text="Supprimer", style="Flat.TButton", command=delete).pack(side="left", padx=6)
        ttk.Button(bb, text="Utiliser cette station", style="Flat.TButton", command=use).pack(side="left")
        ttk.Button(bb, text="Fermer", style="Flat.TButton", command=win.destroy).pack(side="left", padx=6)
        fill()
