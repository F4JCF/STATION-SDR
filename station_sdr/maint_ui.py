"""Maintenance du Pi depuis l'appli : installation des modules en un clic,
chien de garde de la clé, sauvegarde automatique hebdomadaire."""

import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import api_client, theme
from .config import save_config
from .version import __version__

BACKUP_EVERY_DAYS = 7
BACKUP_KEEP = 8


def backup_dir() -> Path:
    docs = Path.home() / "Documents"
    base = docs if docs.is_dir() else Path.home()
    return base / "STATION-SDR" / "Sauvegardes"


class MaintMixin:
    # ------------------------------------------------- Modules du Pi --

    def _open_modules_window(self):
        if getattr(self, "_mod_win", None) is not None and self._mod_win.winfo_exists():
            self._mod_win.lift()
            return
        win = tk.Toplevel(self)
        self._mod_win = win
        win.title("Modules du Pi")
        win.configure(bg=theme.BG)
        win.geometry("980x640")
        tk.Label(win, text="Modules du Pi", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION).pack(
            anchor="w", padx=14, pady=(12, 2))
        tk.Label(win, text=("Un clic installe le module sur le Pi (téléchargement, compilation éventuelle, "
                            "configuration). Comptez de 1 à 30 minutes selon le module ; le Pi reste utilisable."),
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE, justify="left", wraplength=940).pack(
            anchor="w", padx=14)
        self._mod_list = tk.Frame(win, bg=theme.BG)
        self._mod_list.pack(fill="x", padx=14, pady=8)
        tk.Label(win, text="Déroulement de l'installation :", bg=theme.BG, fg=theme.TEXT,
                 font=theme.FONT_BODY_BOLD).pack(anchor="w", padx=14)
        self._mod_log = tk.Text(win, bg=theme.BG_CARD_ALT, fg=theme.TEXT, font=("Consolas", 9), relief="flat",
                                height=14, wrap="word", padx=8, pady=6)
        self._mod_log.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        self._mod_log.config(state="disabled")
        self._modules_refresh()

    def _modules_refresh(self):
        win = getattr(self, "_mod_win", None)
        if win is None or not win.winfo_exists():
            return
        host, port = self._pi_endpoint()

        def worker():
            try:
                data = api_client.get_pi_modules(host, port)
                err = None
            except Exception as exc:
                data, err = None, str(exc)
            self.after(0, self._modules_render, data, err)

        threading.Thread(target=worker, daemon=True).start()

    def _modules_render(self, data, err):
        win = getattr(self, "_mod_win", None)
        if win is None or not win.winfo_exists():
            return
        for c in self._mod_list.winfo_children():
            c.destroy()
        if err:
            tk.Label(self._mod_list, text=(f"Le Pi ne répond pas, ou son service de bascule est ancien : cliquez "
                                           f"d'abord « Mettre à jour le Pi ».\n({err})"),
                     bg=theme.BG, fg=theme.ORANGE, font=theme.FONT_BODY, justify="left", wraplength=940).pack(anchor="w")
            self.after(10000, self._modules_refresh)
            return
        inst = data.get("install") or {}
        running = inst.get("running")
        for i, m in enumerate(data.get("modules") or []):
            row = tk.Frame(self._mod_list, bg=theme.BG_CARD)
            row.pack(fill="x", pady=2)
            ok = m.get("installed")
            tk.Label(row, text="●", bg=theme.BG_CARD, fg=theme.GREEN if ok else theme.MUTED,
                     font=("Segoe UI", 12)).pack(side="left", padx=(10, 6), pady=6)
            tk.Label(row, text=m.get("label"), bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_BODY,
                     width=52, anchor="w").pack(side="left")
            tk.Label(row, text="installé" if ok else "non installé", bg=theme.BG_CARD,
                     fg=theme.GREEN if ok else theme.MUTED, font=theme.FONT_SUBTITLE, width=12).pack(side="left")
            b = ttk.Button(row, text="Réinstaller" if ok else "Installer",
                           style="Flat.TButton" if ok else "Accent.TButton",
                           command=lambda k=m["key"], lab=m["label"]: self._module_install(k, lab))
            b.pack(side="right", padx=8, pady=4)
            if running:
                b.state(["disabled"])
        txt = inst.get("log") or "(aucune installation lancée depuis l'appli)"
        if running:
            txt += "\n… installation en cours …"
        elif inst.get("module") and inst.get("exit_code") is not None:
            txt += ("\n✔ Installation terminée avec succès." if inst.get("exit_code") == 0
                    else f"\n✖ L'installation a échoué (code {inst.get('exit_code')}) : voir le journal ci-dessus.")
        self._mod_log.config(state="normal")
        self._mod_log.delete("1.0", "end")
        self._mod_log.insert("1.0", txt)
        self._mod_log.see("end")
        self._mod_log.config(state="disabled")
        self.after(3000 if running else 15000, self._modules_refresh)

    def _module_install(self, key, label):
        if not messagebox.askyesno("Modules du Pi", f"Installer « {label} » sur le Pi ?\n\nLe module en cours "
                                                    "d'utilisation peut être brièvement interrompu.",
                                   parent=self._mod_win):
            return
        host, port = self._pi_endpoint()

        def worker():
            try:
                api_client.start_pi_install(host, port, key, f"v{__version__}")
                err = None
            except Exception as exc:
                err = str(exc)
                try:
                    err = exc.response.json().get("error") or err  # type: ignore[attr-defined]
                except Exception:
                    pass
            self.after(0, lambda: messagebox.showwarning("Modules du Pi", f"Installation impossible : {err}",
                                                         parent=self._mod_win) if err else None)
            self.after(500, self._modules_refresh)

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------- Chien de garde --

    def _watchdog_fetch(self):
        host, port = self._pi_endpoint()
        try:
            data = api_client.get_pi_watchdog(host, port)
        except Exception:
            data = None
        self.after(0, self._watchdog_render, data)

    def _watchdog_render(self, data):
        lbl = getattr(self, "health_watch_label", None)
        if lbl is None:
            return
        if not data:
            lbl.config(text="Surveillance automatique de la clé : disponible après « Mettre à jour le Pi ».",
                       fg=theme.MUTED)
            return
        ev = data.get("events") or []
        day = [e for e in ev if time.time() - e["time"] < 86400]
        if not ev:
            txt, col = "Surveillance automatique de la clé : active, aucune intervention.", theme.GREEN
        else:
            last = ev[0]
            txt = (f"Surveillance automatique de la clé : {len(day)} intervention(s) sur 24 h — dernière à "
                   f"{time.strftime('%d/%m %H:%M', time.localtime(last['time']))} : {last['text']}")
            col = theme.ORANGE if day else theme.MUTED
        if data.get("dongle_present") is False:
            txt += "  ·  ⚠ clé SDR absente de l'USB en ce moment"
            col = theme.RED
        lbl.config(text=txt, fg=col)

    # ------------------------------------------ Sauvegarde automatique --

    def _auto_backup_tick(self):
        last = float(self.config_data.get("auto_backup_last") or 0)
        if self.config_data.get("auto_backup", True) and time.time() - last > BACKUP_EVERY_DAYS * 86400:
            threading.Thread(target=self._auto_backup_run, daemon=True).start()
        self._auto_backup_show()
        self.after(6 * 3600 * 1000, self._auto_backup_tick)

    def _auto_backup_run(self, manual=False):
        host, port = self._pi_endpoint()
        try:
            data = api_client.get_pi_backup(host, port)
            d = backup_dir()
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"station-sdr-config-{time.strftime('%Y%m%d-%H%M')}.tar.gz"
            path.write_bytes(data)
            files = sorted(d.glob("station-sdr-config-*.tar.gz"))
            for old in files[:-BACKUP_KEEP]:
                try:
                    old.unlink()
                except OSError:
                    pass
            self.config_data["auto_backup_last"] = time.time()
            self.config_data["auto_backup_file"] = str(path)
            try:
                save_config(self.config_data)
            except Exception:
                pass
            err = None
        except Exception as exc:
            err = str(exc)
        self.after(0, self._auto_backup_show, err if manual else None)

    def _auto_backup_show(self, err=None):
        lbl = getattr(self, "health_backup_label", None)
        if lbl is None:
            return
        if err:
            lbl.config(text=f"Sauvegarde impossible : {err}", fg=theme.RED)
            return
        last = self.config_data.get("auto_backup_last")
        if last:
            lbl.config(text=(f"Sauvegarde automatique chaque semaine (8 conservées) — dernière le "
                             f"{time.strftime('%d/%m/%Y à %H:%M', time.localtime(last))}"), fg=theme.MUTED)
        else:
            lbl.config(text="Sauvegarde automatique chaque semaine : la première sera faite dès que le Pi répond.",
                       fg=theme.MUTED)

    def _open_backup_dir(self):
        d = backup_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(d))  # type: ignore[attr-defined]
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(d)])
        except Exception as exc:
            messagebox.showinfo("Sauvegardes", f"Dossier des sauvegardes :\n{d}\n\n({exc})")
