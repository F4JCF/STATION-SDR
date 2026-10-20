"""STATION-SDR - Tableau de bord Windows pour le Raspberry Pi F4JCF-SDR-STATION.

Interface Tkinter simple : un panneau par fonction (météo, trafic aérien,
services système, statut SDR), rafraîchis périodiquement en arrière-plan,
plus une vérification de mise à jour automatique au démarrage.
"""

import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import requests

from . import api_client, updater
from .config import load_config, save_config
from .version import __version__

APP_TITLE = f"STATION-SDR v{__version__}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("820x600")
        self.minsize(700, 500)

        self.config_data = load_config()
        self._stop_event = threading.Event()

        self._build_menu()
        self._build_layout()

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

    def _build_layout(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_weather = self._make_text_tab(notebook, "Météo")
        self.tab_aircraft = self._make_text_tab(notebook, "Trafic aérien (ADS-B)")
        self.tab_services = self._make_text_tab(notebook, "Services")
        self.tab_sdr = self._make_text_tab(notebook, "Récepteurs SDR")

        status_bar = ttk.Frame(self)
        status_bar.pack(fill="x", padx=8, pady=(0, 8))
        self.status_label = ttk.Label(status_bar, text="Démarrage...", anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)

    def _make_text_tab(self, notebook, label):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=label)
        text = tk.Text(frame, wrap="word", state="disabled", font=("Consolas", 10))
        text.pack(fill="both", expand=True, padx=6, pady=6)
        return text

    def _set_tab_content(self, text_widget, content: str):
        text_widget.configure(state="normal")
        text_widget.delete("1.0", "end")
        text_widget.insert("1.0", content)
        text_widget.configure(state="disabled")

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

        self._fetch_and_render(
            lambda: api_client.get_weather(host, cfg["port_weather"]),
            self.tab_weather,
            self._format_weather,
        )
        self._fetch_and_render(
            lambda: api_client.get_aircraft(host, cfg["port_adsb"]),
            self.tab_aircraft,
            self._format_aircraft,
        )
        self._fetch_and_render(
            lambda: api_client.get_services_status(host, cfg["port_services"]),
            self.tab_services,
            self._format_services,
        )
        self._fetch_and_render(
            lambda: api_client.get_sdr_status(host, cfg["port_sdr"]),
            self.tab_sdr,
            self._format_sdr,
        )

        self._set_status(f"Dernière mise à jour : {time.strftime('%H:%M:%S')} (hôte {host})")

    def _fetch_and_render(self, fetch_fn, text_widget, format_fn):
        try:
            data = fetch_fn()
            content = format_fn(data)
        except requests.RequestException as exc:
            content = f"Indisponible : {exc}"
        except Exception as exc:  # garde-fou : un panneau ne doit jamais planter l'appli
            content = f"Erreur inattendue : {exc}"
        self.after(0, self._set_tab_content, text_widget, content)

    def _set_status(self, text: str):
        self.after(0, lambda: self.status_label.config(text=text))

    # ----------------------------------------------------------- Format --

    @staticmethod
    def _format_weather(data: dict) -> str:
        lines = ["Météo locale", "=" * 40]
        for key, value in data.items():
            lines.append(f"{key} : {value}")
        return "\n".join(lines)

    @staticmethod
    def _format_aircraft(data: dict) -> str:
        aircraft = data.get("aircraft", [])
        lines = [f"Avions détectés : {len(aircraft)}", "=" * 40]
        for ac in aircraft:
            flight = (ac.get("flight") or "").strip() or ac.get("hex", "?")
            alt = ac.get("alt_baro", "?")
            gs = ac.get("gs", "?")
            lines.append(f"{flight:<10} alt={alt!s:<8} vitesse={gs!s:<6} hex={ac.get('hex', '')}")
        return "\n".join(lines)

    @staticmethod
    def _format_services(data: dict) -> str:
        lines = ["Services système", "=" * 40]
        for key, info in data.items():
            label = info.get("label", key)
            running = info.get("running")
            state = "EN COURS" if running else "ARRÊTÉ"
            lines.append(f"{label:<30} {state}")
        return "\n".join(lines)

    @staticmethod
    def _format_sdr(data: dict) -> str:
        lines = ["Utilisation des récepteurs SDR", "=" * 40]
        for key, in_use in data.items():
            state = "OCCUPÉ" if in_use else "LIBRE"
            lines.append(f"{key:<15} {state}")
        return "\n".join(lines)

    # ------------------------------------------------------- Paramètres --

    def _open_settings(self):
        host = simpledialog.askstring(
            "Paramètres", "Adresse IP du Raspberry Pi :",
            initialvalue=self.config_data.get("pi_host", ""), parent=self,
        )
        if host:
            self.config_data["pi_host"] = host.strip()

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
