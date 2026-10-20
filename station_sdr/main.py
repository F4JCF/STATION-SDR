"""STATION-SDR - Tableau de bord Windows pour le Raspberry Pi F4JCF-SDR-STATION.

Interface Tkinter : bandeau d'en-tête, puces de statut global, onglets en
carte (météo, trafic aérien, services, récepteurs SDR), rafraîchis
périodiquement en arrière-plan, plus mise à jour automatique au démarrage.
"""

import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import requests
from tkintermapview import TkinterMapView

from . import api_client, theme, updater
from .config import load_config, save_config
from .version import __version__

APP_TITLE = f"STATION-SDR v{__version__}"

# Coordonnées connues des stations météo (code OACI -> lat, lon), avec un
# repli sur Mougins si le code renvoyé par l'API n'est pas reconnu.
STATION_COORDS = {
    "LFMD": (43.5417, 6.9533),  # Cannes - Mandelieu
    "LFMN": (43.6584, 7.2159),  # Nice - Côte d'Azur
}
DEFAULT_COORDS = (43.6045, 6.9971)  # Mougins


def _kt_to_kmh(value):
    try:
        return round(float(value) * 1.852, 1)
    except (TypeError, ValueError):
        return None


def _fmt(value):
    return value if value not in (None, "") else "—"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1040x680")
        self.minsize(860, 560)

        self.config_data = load_config()
        self._stop_event = threading.Event()
        self._last_weather = {}
        self._last_services = {}

        theme.apply_dark_theme(self)
        self._build_menu()
        self._build_header()
        self._build_body()
        self._tick_clock()

        self.after(200, self._check_update_on_startup)
        self._start_refresh_loop()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- UI --

    def _build_menu(self):
        menubar = tk.Menu(self)
        app_menu = tk.Menu(menubar, tearoff=0)
        app_menu.add_command(label="Paramètres...", command=self._open_settings)
        app_menu.add_command(label="Vérifier les mises à jour", command=self._manual_update_check)
        app_menu.add_separator()
        app_menu.add_command(label="Quitter", command=self._on_close)
        menubar.add_cascade(label="STATION-SDR", menu=app_menu)
        self.config(menu=menubar)

    def _build_header(self):
        header = tk.Frame(self, bg=theme.BG)
        header.pack(fill="x", padx=20, pady=(16, 8))

        left = tk.Frame(header, bg=theme.BG)
        left.pack(side="left", fill="y")
        tk.Label(left, text="STATION-SDR", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_TITLE).pack(anchor="w")
        tk.Label(
            left, text="Supervision F4JCF-SDR-STATION — ADS-B · Météo · APRS · SDR",
            bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE,
        ).pack(anchor="w")

        right = tk.Frame(header, bg=theme.BG)
        right.pack(side="right", fill="y")
        self.clock_label = tk.Label(right, text="--:--:--", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_TITLE)
        self.clock_label.pack(anchor="e")
        self.date_label = tk.Label(right, text="", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.date_label.pack(anchor="e")

        # Puces de statut global
        strip = tk.Frame(self, bg=theme.BG)
        strip.pack(fill="x", padx=20, pady=(0, 10))

        self.chip_host = theme.chip(strip, f"Hôte {self.config_data.get('pi_host', '?')}")
        self.chip_host.pack(side="left", padx=(0, 8))
        self.chip_aircraft = theme.chip(strip, "Avions : —")
        self.chip_aircraft.pack(side="left", padx=8)
        self.chip_services = theme.chip(strip, "Services : —")
        self.chip_services.pack(side="left", padx=8)
        self.chip_sdr = theme.chip(strip, "SDR : —")
        self.chip_sdr.pack(side="left", padx=8)
        self.chip_updated = theme.chip(strip, "Maj : —")
        self.chip_updated.pack(side="right")

    def _build_body(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        self.tab_weather = self._add_tab(notebook, "Météo")
        self.tab_aircraft = self._add_tab(notebook, "Trafic aérien (ADS-B)")
        self.tab_services = self._add_tab(notebook, "Services")
        self.tab_sdr = self._add_tab(notebook, "Récepteurs SDR")

        self._build_weather_tab(self.tab_weather)
        self._build_aircraft_table(self.tab_aircraft)

    def _add_tab(self, notebook, label):
        frame = ttk.Frame(notebook, style="TFrame")
        notebook.add(frame, text=label)
        return frame

    def _clear(self, frame):
        for child in frame.winfo_children():
            child.destroy()

    # --------------------------------------------------------- Horloge --

    def _tick_clock(self):
        now = time.localtime()
        self.clock_label.config(text=time.strftime("%H:%M:%S", now))
        self.date_label.config(text=time.strftime("%A %d %B %Y", now))
        self.after(1000, self._tick_clock)

    # ------------------------------------------------------------ Réseau --

    def _start_refresh_loop(self):
        thread = threading.Thread(target=self._refresh_loop, daemon=True)
        thread.start()

    def _refresh_loop(self):
        while not self._stop_event.is_set():
            self._refresh_once()
            self._stop_event.wait(self.config_data.get("refresh_seconds", 15))

    def _refresh_once(self):
        host = self.config_data.get("pi_host", "192.168.1.229")
        cfg = self.config_data

        self._fetch(lambda: api_client.get_weather(host, cfg["port_weather"]), self._render_weather)
        self._fetch(lambda: api_client.get_aircraft(host, cfg["port_adsb"]), self._render_aircraft)
        self._fetch(lambda: api_client.get_services_status(host, cfg["port_services"]), self._render_services)
        self._fetch(lambda: api_client.get_sdr_status(host, cfg["port_sdr"]), self._render_sdr)

        self.after(0, lambda: self.chip_updated.config(text=f"Maj : {time.strftime('%H:%M:%S')}"))

    def _fetch(self, fetch_fn, render_fn):
        try:
            data = fetch_fn()
        except requests.RequestException as exc:
            self.after(0, render_fn, None, str(exc))
            return
        except Exception as exc:  # garde-fou : un panneau ne doit jamais planter l'appli
            self.after(0, render_fn, None, f"Erreur inattendue : {exc}")
            return
        self.after(0, render_fn, data, None)

    def _render_error(self, frame, message):
        self._clear(frame)
        tk.Label(
            frame, text=f"Indisponible — {message}", bg=theme.BG, fg=theme.RED,
            font=theme.FONT_BODY, wraplength=800, justify="left",
        ).pack(anchor="w", padx=16, pady=16)

    # --------------------------------------------------------------- Météo --

    def _build_weather_tab(self, frame):
        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        outer = theme.card(wrapper)
        outer.pack(fill="x", padx=12, pady=12)
        inner = outer.inner
        inner.configure(padding=16)

        self.weather_station_label = tk.Label(
            inner, text="Station : —", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION,
        )
        self.weather_station_label.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))

        row_titles = [
            ("temperature", "Température"), ("vent", "Vent"),
            ("rafales", "Rafales"), ("visibilite", "Visibilité"),
            ("pression", "Pression QNH"), ("point_rosee", "Point de rosée"),
            ("observation", "Observation"), ("source", "Source"),
        ]
        self.weather_value_labels = {}
        for i, (key, title) in enumerate(row_titles):
            r, c = divmod(i, 2)
            cell = tk.Frame(inner, bg=theme.BG_CARD)
            cell.grid(row=r + 1, column=c, sticky="w", padx=(0, 40), pady=4)
            tk.Label(cell, text=title, bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE).pack(anchor="w")
            value_label = tk.Label(cell, text="—", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY_BOLD)
            value_label.pack(anchor="w")
            self.weather_value_labels[key] = value_label

        self.weather_clouds_label = tk.Label(
            inner, text="", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY, justify="left",
        )
        self.weather_clouds_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.weather_metar_label = tk.Label(
            inner, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_MONO,
            wraplength=820, justify="left",
        )
        self.weather_metar_label.grid(row=6, column=0, columnspan=4, sticky="w", pady=(14, 0))

        map_outer = theme.card(wrapper)
        map_outer.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.weather_map = TkinterMapView(map_outer.inner, corner_radius=0)
        self.weather_map.pack(fill="both", expand=True, padx=1, pady=1)
        lat, lon = DEFAULT_COORDS
        self.weather_map.set_position(lat, lon)
        self.weather_map.set_zoom(11)
        self.weather_map.set_marker(lat, lon, text="Station")
        self._weather_map_station = None

    def _render_weather(self, data, error):
        if error:
            self.weather_station_label.config(text="Station : indisponible")
            for lbl in self.weather_value_labels.values():
                lbl.config(text="—")
            self.weather_clouds_label.config(text="")
            self.weather_metar_label.config(text=f"Indisponible — {error}", fg=theme.RED)
            return

        self._last_weather = data
        self.weather_metar_label.config(fg=theme.MUTED)

        station = data.get("station") or data.get("nom") or "—"
        self.weather_station_label.config(text=f"Station : {station}")

        vent_kt = data.get("vent_vitesse_kt")
        rafales_kt = data.get("vent_rafales_kt")
        values = {
            "temperature": f"{data.get('temperature_c', '—')} °C",
            "vent": f"{data.get('vent_direction_deg', '—')}° — "
                    f"{_fmt(vent_kt)} kt ({_fmt(_kt_to_kmh(vent_kt))} km/h)",
            "rafales": f"{_fmt(rafales_kt)} kt ({_fmt(_kt_to_kmh(rafales_kt))} km/h)",
            "visibilite": f"{data.get('visibilite_sm', '—')} sm",
            "pression": f"{data.get('pression_qnh_hpa', '—')} hPa",
            "point_rosee": f"{data.get('point_rosee_c', '—')} °C",
            "observation": data.get("heure_obs", "—"),
            "source": data.get("source", "—"),
        }
        for key, value in values.items():
            self.weather_value_labels[key].config(text=value)

        nuages = data.get("nuages") or []
        if nuages:
            txt = " · ".join(f"{n.get('cover', '?')} {n.get('base', '?')} ft" for n in nuages)
            self.weather_clouds_label.config(text=f"Nuages : {txt}")
        else:
            self.weather_clouds_label.config(text="")

        self.weather_metar_label.config(text=data.get("metar_brut", ""))

        if station != self._weather_map_station:
            lat, lon = STATION_COORDS.get(station, DEFAULT_COORDS)
            self.weather_map.set_position(lat, lon)
            self.weather_map.delete_all_marker()
            self.weather_map.set_marker(lat, lon, text=station)
            self._weather_map_station = station

    # ------------------------------------------------------------- ADS-B --

    def _build_aircraft_table(self, frame):
        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        self.aircraft_count_label = tk.Label(
            wrapper, text="Avions détectés : —", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION,
        )
        self.aircraft_count_label.pack(anchor="w", padx=12, pady=(8, 6))

        table_outer = theme.card(wrapper)
        table_outer.pack(fill="both", padx=12, pady=(0, 8))
        columns = ("flight", "hex", "alt", "gs", "track", "squawk")
        self.aircraft_tree = ttk.Treeview(table_outer.inner, columns=columns, show="headings", height=7)
        headings = {
            "flight": "Indicatif", "hex": "Hex", "alt": "Altitude (ft)",
            "gs": "Vitesse (kt)", "track": "Cap (°)", "squawk": "Squawk",
        }
        for col in columns:
            self.aircraft_tree.heading(col, text=headings[col])
            self.aircraft_tree.column(col, width=130, anchor="center")
        self.aircraft_tree.pack(fill="both", expand=True, padx=1, pady=1)

        map_outer = theme.card(wrapper)
        map_outer.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.aircraft_map = TkinterMapView(map_outer.inner, corner_radius=0)
        self.aircraft_map.pack(fill="both", expand=True, padx=1, pady=1)
        lat, lon = DEFAULT_COORDS
        self.aircraft_map.set_position(lat, lon)
        self.aircraft_map.set_zoom(8)

    def _render_aircraft(self, data, error):
        if error:
            self.aircraft_count_label.config(text="Avions détectés : indisponible")
            for item in self.aircraft_tree.get_children():
                self.aircraft_tree.delete(item)
            self.aircraft_map.delete_all_marker()
            return

        aircraft = data.get("aircraft", [])
        self.aircraft_count_label.config(text=f"Avions détectés : {len(aircraft)}")
        for item in self.aircraft_tree.get_children():
            self.aircraft_tree.delete(item)
        self.aircraft_map.delete_all_marker()
        for ac in aircraft:
            flight = (ac.get("flight") or "").strip() or "—"
            self.aircraft_tree.insert(
                "", "end",
                values=(
                    flight, ac.get("hex", "—"), ac.get("alt_baro", "—"),
                    ac.get("gs", "—"), ac.get("track", "—"), ac.get("squawk", "—"),
                ),
            )
            lat, lon = ac.get("lat"), ac.get("lon")
            if lat is not None and lon is not None:
                alt = ac.get("alt_baro", "—")
                self.aircraft_map.set_marker(lat, lon, text=f"{flight} · {alt} ft")

    # ---------------------------------------------------------- Services --

    def _render_services(self, data, error):
        frame = self.tab_services
        if error:
            self._render_error(frame, error)
            return
        self._last_services = data
        self._clear(frame)

        total = len(data)
        running = sum(1 for v in data.values() if v.get("running"))
        self.chip_services.config(text=f"Services : {running}/{total}")

        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        outer = theme.card(wrapper)
        outer.pack(fill="both", expand=True, padx=12, pady=12)
        inner = outer.inner
        inner.configure(padding=12)

        for i, (key, info) in enumerate(sorted(data.items())):
            label = info.get("label", key)
            is_running = bool(info.get("running"))

            row = tk.Frame(inner, bg=theme.BG_CARD_ALT if i % 2 else theme.BG_CARD)
            row.pack(fill="x", pady=1)

            dot = theme.status_dot(row, theme.GREEN if is_running else theme.RED)
            dot.configure(bg=row["bg"])
            dot.pack(side="left", padx=(10, 8), pady=8)

            tk.Label(
                row, text=label, bg=row["bg"], fg=theme.TEXT, font=theme.FONT_BODY,
            ).pack(side="left", padx=(0, 12), pady=8)

            badge = theme.pill(row, "EN COURS" if is_running else "ARRÊTÉ", theme.GREEN if is_running else theme.RED)
            badge.pack(side="right", padx=10, pady=6)

            restart_btn = ttk.Button(
                row, text="Redémarrer", style="Flat.TButton",
                command=lambda k=key: self._restart_service(k),
            )
            restart_btn.pack(side="right", padx=(0, 10), pady=6)

    def _restart_service(self, key):
        host = self.config_data.get("pi_host", "192.168.1.229")
        port = self.config_data["port_services"]

        def _do():
            try:
                api_client.restart_service(host, port, key)
            except requests.RequestException as exc:
                self.after(0, messagebox.showerror, "Redémarrage", f"Échec du redémarrage de {key} :\n{exc}")
                return
            self.after(500, self._refresh_once)

        threading.Thread(target=_do, daemon=True).start()

    # ---------------------------------------------------------------- SDR --

    def _render_sdr(self, data, error):
        frame = self.tab_sdr
        if error:
            self._render_error(frame, error)
            return
        self._clear(frame)

        in_use = sum(1 for v in data.values() if v)
        self.chip_sdr.config(text=f"SDR : {in_use}/{len(data)} occupé(s)")

        wrapper = tk.Frame(frame, bg=theme.BG)
        wrapper.pack(fill="both", expand=True, padx=4, pady=4)

        grid = tk.Frame(wrapper, bg=theme.BG)
        grid.pack(fill="both", expand=True, padx=8, pady=8)

        names = {"adsb": "ADS-B", "ais": "AIS", "aprs": "APRS", "sdr": "Récepteur SDR"}
        for i, (key, used) in enumerate(sorted(data.items())):
            outer = theme.card(grid)
            outer.grid(row=i // 2, column=i % 2, sticky="nsew", padx=10, pady=10)
            grid.grid_columnconfigure(i % 2, weight=1)
            inner = outer.inner
            inner.configure(padding=16)
            tk.Label(
                inner, text=names.get(key, key), bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION,
            ).pack(anchor="w")
            badge = theme.pill(inner, "OCCUPÉ" if used else "LIBRE", theme.ORANGE if used else theme.GREEN)
            badge.pack(anchor="w", pady=(10, 0))

    # ------------------------------------------------------- Paramètres --

    def _open_settings(self):
        host = simpledialog.askstring(
            "Paramètres", "Adresse IP du Raspberry Pi :",
            initialvalue=self.config_data.get("pi_host", ""), parent=self,
        )
        if host:
            self.config_data["pi_host"] = host.strip()
            self.chip_host.config(text=f"Hôte {self.config_data['pi_host']}")

        token = simpledialog.askstring(
            "Paramètres",
            "Jeton GitHub (optionnel, pour la mise à jour automatique\n"
            "du dépôt privé F4JCF/STATION-SDR) :",
            initialvalue=self.config_data.get("github_token", ""), parent=self, show="*",
        )
        if token is not None:
            self.config_data["github_token"] = token.strip()

        save_config(self.config_data)
        messagebox.showinfo("Paramètres", "Paramètres enregistrés.")

    # --------------------------------------------------------- Updater --

    def _check_update_on_startup(self):
        if not self.config_data.get("check_updates", True):
            return
        threading.Thread(target=self._run_update_check, kwargs={"silent": True}, daemon=True).start()

    def _manual_update_check(self):
        threading.Thread(target=self._run_update_check, kwargs={"silent": False}, daemon=True).start()

    def _run_update_check(self, silent: bool):
        token = self.config_data.get("github_token", "")
        try:
            info = updater.check_for_update(token)
        except requests.RequestException as exc:
            if not silent:
                self.after(0, messagebox.showerror, "Mise à jour",
                            f"Impossible de vérifier les mises à jour :\n{exc}")
            return

        if not info:
            if not silent:
                self.after(0, messagebox.showinfo, "Mise à jour", "Vous utilisez déjà la dernière version.")
            return

        self.after(0, self._prompt_update, info, token)

    def _prompt_update(self, info: dict, token: str):
        answer = messagebox.askyesno(
            "Mise à jour disponible",
            f"Une nouvelle version est disponible : {info['tag']}\n\n"
            f"{info.get('notes', '')}\n\nInstaller maintenant ?",
        )
        if not answer:
            return
        threading.Thread(target=self._download_and_apply_update, args=(info, token), daemon=True).start()

    def _download_and_apply_update(self, info: dict, token: str):
        import os
        import tempfile

        dest = os.path.join(tempfile.gettempdir(), info["asset_name"])
        try:
            updater.download_asset(info["asset_url"], token, dest)
        except requests.RequestException as exc:
            self.after(0, messagebox.showerror, "Mise à jour",
                        f"Échec du téléchargement :\n{exc}")
            return
        self.after(0, lambda: updater.apply_update_and_restart(dest))

    # -------------------------------------------------------------- Fin --

    def _on_close(self):
        self._stop_event.set()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
