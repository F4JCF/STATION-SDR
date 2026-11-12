"""Page « WEFAX » : cartes météo HF en fac-similé (DWD Hambourg, Northwood…).

Deux sources :
  - le PC : le son du FT-991A (carte son USB) est décodé en direct ; la
    fréquence peut être réglée par CAT ;
  - le Pi : avec un convertisseur HF devant la clé SDR, réception
    automatique à heures fixes (créneaux), images dans la galerie.
"""

import queue
import re
from urllib.parse import unquote
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import numpy as np
import requests
from PIL import Image, ImageTk

from . import theme, wefax
from .config import save_config

# Fréquences publiées (kHz) ; la radio se règle 1,9 kHz plus bas, en USB.
STATIONS = (
    ("DWD Hambourg (DDH3) — 3855 kHz", 3855.0),
    ("DWD Hambourg (DDK3) — 7880 kHz", 7880.0),
    ("DWD Hambourg (DDK6) — 13882,5 kHz", 13882.5),
    ("Northwood (GYA) — 2618,5 kHz", 2618.5),
    ("Northwood (GYA) — 4610 kHz", 4610.0),
    ("Northwood (GYA) — 8040 kHz", 8040.0),
    ("Northwood (GYA) — 11086,5 kHz", 11086.5),
)


def wefax_dir() -> Path:
    docs = Path.home() / "Documents"
    return (docs if docs.is_dir() else Path.home()) / "STATION-SDR" / "Wefax"


def shear(img: np.ndarray, px_per_line: float, offset: int) -> np.ndarray:
    """Redresse une image penchée (dérive d'horloge) et la recale horizontalement."""
    out = np.empty_like(img)
    for i in range(img.shape[0]):
        out[i] = np.roll(img[i], -int(round(i * px_per_line)) - offset)
    return out


DWD_DIR = "https://opendata.dwd.de/weather/charts/analysis/"
DWD_KEY = "ana_bwkman_dwdna"          # analyse en surface (manuelle) Atlantique Nord – Europe
DWD_LATEST = "Z__C_EDZW_LATEST_tka01,ana_bwkman_dwdna_O_000000_000000_LATEST_WV12.png"   # repli


def dwd_latest_files(html: str):
    """Dernière analyse publiée : [(heure d'analyse AAAAMMJJHHMM, nom du fichier)].
    Le DWD publie chaque analyse (00, 06, 12, 18 UTC) en couleur (…_WV12.png) et
    en noir et blanc (…_WV12SW.png, comme le fax) : on prend le noir et blanc."""
    found = {}
    for href in re.findall(r'href="([^"]+)"', html):
        name = unquote(href).rsplit("/", 1)[-1]
        m = re.search(r"^Z__C_EDZW_\d{14}_tka01," + DWD_KEY + r"_O_000000_000000_(\d{12})_WV12(SW)?\.png$", name)
        if m:
            found.setdefault(m.group(1), {})["nb" if m.group(2) else "couleur"] = name
    if not found:
        return []
    stamp = max(found)
    v = found[stamp]
    return [(stamp, v.get("nb") or v["couleur"])]


def dwd_download_latest(dest: Path):
    """Télécharge la dernière analyse DWD dans dest. Renvoie (message, nouvelle ?)."""
    try:
        from .map_tiles import USER_AGENT
    except Exception:
        USER_AGENT = "STATION-SDR"
    hdr = {"User-Agent": USER_AGENT}
    r = requests.get(DWD_DIR, headers=hdr, timeout=20)
    r.raise_for_status()
    files = dwd_latest_files(r.text)
    if not files:
        # Repli : la carte « LATEST » (nom fixe), datée par l'en-tête Last-Modified
        h = requests.head(DWD_DIR + DWD_LATEST, headers=hdr, timeout=20)
        if not h.ok:
            return "Aucune carte trouvée sur le serveur du DWD.", False
        from email.utils import parsedate_to_datetime
        try:
            stamp = parsedate_to_datetime(h.headers["Last-Modified"]).strftime("%Y%m%d%H%M")
        except Exception:
            stamp = time.strftime("%Y%m%d%H%M", time.gmtime())
        files = [(stamp, DWD_LATEST)]
    dest.mkdir(parents=True, exist_ok=True)
    stamp = files[0][0]
    when = f"{stamp[6:8]}/{stamp[4:6]} {stamp[8:10]}:{stamp[10:12]} UTC"
    new = False
    for i, (_st, name) in enumerate(files):
        out = dest / (f"DWD_{stamp[:8]}-{stamp[8:12]}" + (f"_{i + 1}" if i else "") + ".png")
        if out.exists():
            continue
        data = requests.get(DWD_DIR + name.replace(",", "%2C"), headers=hdr, timeout=60)
        data.raise_for_status()
        tmp = out.with_suffix(".part")
        tmp.write_bytes(data.content)
        tmp.replace(out)
        new = True
    # on garde les 12 dernières cartes DWD (les cartes reçues par radio ne sont pas touchées)
    for old in sorted(dest.glob("DWD_*.png"), reverse=True)[12:]:
        try:
            old.unlink()
        except OSError:
            pass
    return (f"Nouvelle carte DWD ajoutée : analyse du {when}." if new
            else f"Déjà à jour : dernière analyse DWD du {when}."), new


class WefaxMixin:
    # ------------------------------------------------------------ Page
    def _build_wefax_tab(self, frame):
        cfg = self.config_data
        outer = tk.Frame(frame, bg=theme.BG)
        outer.pack(fill="both", expand=True, padx=4, pady=4)
        c = theme.card(outer)
        c.pack(fill="x", padx=8, pady=(8, 6))
        h = c.inner
        h.configure(padding=12)
        top = tk.Frame(h, bg=theme.BG_CARD)
        top.pack(fill="x")
        tk.Label(top, text="WEFAX — cartes météo HF (FT-991A + PC)", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_SECTION).pack(side="left")
        self.wf_state = tk.Label(top, text="  ARRÊTÉ  ", bg=theme.MUTED, fg="#0b0f17", font=theme.FONT_CHIP)
        self.wf_state.pack(side="left", padx=10)
        self.wf_info = tk.Label(top, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.wf_info.pack(side="left", padx=6)
        r1 = tk.Frame(h, bg=theme.BG_CARD)
        r1.pack(fill="x", pady=(8, 0))
        lab = dict(bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        tk.Label(r1, text="Station :", **lab).pack(side="left")
        self.wf_station = tk.StringVar(value=cfg.get("wefax_station", STATIONS[1][0]))
        cb = ttk.Combobox(r1, textvariable=self.wf_station, values=[s[0] for s in STATIONS], width=34,
                          state="readonly")
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._wf_station_changed())
        tk.Label(r1, text="ou kHz :", **lab).pack(side="left", padx=(8, 4))
        self.wf_khz = tk.StringVar(value=str(cfg.get("wefax_khz", 7880.0)).replace(".", ","))
        ttk.Entry(r1, textvariable=self.wf_khz, width=9).pack(side="left")
        self.wf_dial = tk.Label(r1, text="", bg=theme.BG_CARD, fg=theme.ACCENT, font=theme.FONT_BODY_BOLD)
        self.wf_dial.pack(side="left", padx=10)
        self.wf_khz.trace_add("write", lambda *_a: self._wf_update_dial())
        r2 = tk.Frame(h, bg=theme.BG_CARD)
        r2.pack(fill="x", pady=(6, 0))
        tk.Label(r2, text="Entrée son :", **lab).pack(side="left")
        self.wf_dev = tk.StringVar(value=cfg.get("wefax_audio", ""))
        self.wf_dev_cb = ttk.Combobox(r2, textvariable=self.wf_dev, width=40, state="readonly")
        self.wf_dev_cb.pack(side="left", padx=6)
        tk.Label(r2, text="CAT :", **lab).pack(side="left", padx=(10, 4))
        self.wf_port = tk.StringVar(value=cfg.get("cat_port", "COM13"))
        ttk.Entry(r2, textvariable=self.wf_port, width=8).pack(side="left")
        self.wf_baud = tk.StringVar(value=str(cfg.get("cat_baud", 38400)))
        ttk.Combobox(r2, textvariable=self.wf_baud, width=7, values=("4800", "9600", "19200", "38400"),
                     state="readonly").pack(side="left", padx=4)
        ttk.Button(r2, text="Régler la radio", style="Flat.TButton", command=self._wf_cat_tune).pack(side="left",
                                                                                                   padx=6)
        self.wf_btn = ttk.Button(r2, text="▶ Écouter", style="Accent.TButton", command=self._wf_toggle)
        self.wf_btn.pack(side="left", padx=(10, 0))
        ttk.Button(r2, text="Terminer la carte", style="Flat.TButton",
                   command=lambda: self._wf_dec and self._wf_dec.flush()).pack(side="left", padx=6)
        tk.Label(h, bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", theme.fs(8)), justify="left", wraplength=theme.px(1500),
                 text=("La radio se règle en USB (DATA-U) 1,9 kHz sous la fréquence publiée. Fermez VarAC, WSJT-X ou "
                       "Winlink avant « Régler la radio » (le port CAT ne peut servir qu'à un logiciel à la fois). "
                       "Chaque carte commence par une tonalité et un phasage de 30 s ; une carte prise en cours est "
                       "décodée aussi. Programmes horaires : DWD « Seefunk-Faxprogramm », Northwood « Metfax "
                       "schedule ».")).pack(anchor="w", pady=(6, 0))
        body = tk.Frame(outer, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        left = theme.card(body)
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left.inner, text="Réception en cours", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_SECTION).pack(anchor="w", padx=10, pady=(8, 0))
        self.wf_canvas = tk.Canvas(left.inner, bg="#05070f", highlightthickness=0)
        self.wf_canvas.pack(fill="both", expand=True, padx=10, pady=8)
        right = tk.Frame(body, bg=theme.BG, width=theme.px(560))
        right.pack(side="left", fill="y", padx=(10, 0))
        right.pack_propagate(False)
        tk.Label(right, text="Cartes reçues (clic : ouvrir)", bg=theme.BG, fg=theme.TEXT,
                 font=theme.FONT_SECTION).pack(anchor="w")
        self.wf_gallery = tk.Frame(right, bg=theme.BG)
        self.wf_gallery.pack(fill="both", expand=True, pady=(4, 0))
        net = theme.card(right)
        net.pack(fill="x", pady=(8, 0))
        self._build_wefax_dwd(net.inner)
        pi = theme.card(right)
        pi.pack(fill="x", pady=(8, 0))
        self._build_wefax_pi(pi.inner)
        self._wf_dec = None
        self._wf_stream = None
        self._wf_q = queue.Queue(maxsize=200)
        self._wf_rows = []
        self._wf_photo = None
        self._wf_thumbs = []
        self._wf_update_dial()
        self.after(1500, self._wf_list_devices)
        self.after(2500, self._wf_refresh_gallery)
        self.after(8000, self._dwd_auto_tick)

    # ------------------------------------------------ Internet (DWD)
    def _build_wefax_dwd(self, f):
        f.configure(padding=10)
        tk.Label(f, text="Par Internet : cartes du DWD (sans radio)", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_BODY_BOLD).pack(anchor="w")
        r = tk.Frame(f, bg=theme.BG_CARD)
        r.pack(fill="x", pady=(4, 0))
        ttk.Button(r, text="Télécharger la dernière carte", style="Accent.TButton",
                   command=lambda: self._dwd_fetch(manual=True)).pack(side="left")
        self.dwd_auto = tk.BooleanVar(value=bool(self.config_data.get("wefax_dwd_auto", True)))
        tk.Checkbutton(r, text="Automatique", variable=self.dwd_auto, command=self._dwd_auto_changed,
                       bg=theme.BG_CARD, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT,
                       activebackground=theme.BG_CARD, activeforeground=theme.TEXT, font=theme.FONT_BODY,
                       highlightthickness=0).pack(side="left", padx=10)
        self.dwd_info = tk.Label(f, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", theme.fs(8)),
                                 wraplength=theme.px(520), justify="left")
        self.dwd_info.pack(anchor="w", pady=(4, 0))
        tk.Label(f, bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", theme.fs(8)), justify="left",
                 wraplength=theme.px(520),
                 text=("Analyse en surface Atlantique Nord – Europe, la même que celle du fax de Hambourg, "
                       "téléchargée sur le serveur public du Deutscher Wetterdienst (opendata.dwd.de). "
                       "En automatique, une nouvelle carte est cherchée toutes les heures (environ 4 par jour).")
                 ).pack(anchor="w", pady=(4, 0))
        self._dwd_busy = False

    def _dwd_auto_changed(self):
        self.config_data["wefax_dwd_auto"] = bool(self.dwd_auto.get())
        try:
            save_config(self.config_data)
        except Exception:
            pass
        if self.dwd_auto.get():
            self._dwd_fetch(manual=False)

    def _dwd_auto_tick(self):
        if self.dwd_auto.get():
            self._dwd_fetch(manual=False)
        self.after(60 * 60 * 1000, self._dwd_auto_tick)

    def _dwd_fetch(self, manual=False):
        if self._dwd_busy:
            return
        self._dwd_busy = True
        if manual:
            self.dwd_info.config(text="Recherche de la dernière carte…")

        def worker():
            try:
                msg, new = dwd_download_latest(wefax_dir())
            except Exception as exc:
                msg, new = f"Téléchargement impossible : {exc}", False
            self.after(0, self._dwd_done, msg, new, manual)

        threading.Thread(target=worker, daemon=True).start()

    def _dwd_done(self, msg, new, manual):
        self._dwd_busy = False
        if manual or new or not self.dwd_info.cget("text"):
            self.dwd_info.config(text=msg)
        if new:
            self._wf_refresh_gallery()

    def _build_wefax_pi(self, f):
        f.configure(padding=10)
        tk.Label(f, text="Réception par le Pi (convertisseur HF requis)", bg=theme.BG_CARD, fg=theme.TEXT,
                 font=theme.FONT_BODY_BOLD).pack(anchor="w")
        cbs = dict(bg=theme.BG_CARD, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG_CARD,
                   activeforeground=theme.TEXT, font=theme.FONT_BODY, highlightthickness=0)
        self.wfp_on = tk.BooleanVar(value=False)
        tk.Checkbutton(f, text="Créneaux automatiques", variable=self.wfp_on, **cbs).pack(anchor="w")
        r = tk.Frame(f, bg=theme.BG_CARD)
        r.pack(fill="x")
        tk.Label(r, text="Décalage du convertisseur (MHz) :", bg=theme.BG_CARD, fg=theme.MUTED,
                 font=theme.FONT_SUBTITLE).pack(side="left")
        self.wfp_off = tk.StringVar(value="125")
        ttk.Entry(r, textvariable=self.wfp_off, width=6).pack(side="left", padx=4)
        tk.Label(f, text="Créneaux (une ligne : HH:MM  durée-min  kHz) :", bg=theme.BG_CARD, fg=theme.MUTED,
                 font=theme.FONT_SUBTITLE).pack(anchor="w", pady=(4, 0))
        self.wfp_slots = tk.Text(f, height=4, width=34, bg=theme.BG_CARD_ALT, fg=theme.TEXT, font=theme.FONT_MONO,
                                 relief="flat", insertbackground=theme.TEXT)
        self.wfp_slots.pack(fill="x")
        rb = tk.Frame(f, bg=theme.BG_CARD)
        rb.pack(fill="x", pady=(4, 0))
        ttk.Button(rb, text="Appliquer", style="Flat.TButton", command=self._wfp_apply).pack(side="left")
        self.wfp_info = tk.Label(rb, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", theme.fs(8)),
                                 wraplength=theme.px(380), justify="left")
        self.wfp_info.pack(side="left", padx=8)
        tk.Label(f, bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", theme.fs(8)), justify="left", wraplength=theme.px(520),
                 text=("La clé SDR ne descend pas sous 25 MHz : il faut un convertisseur HF (Ham It Up : 125 MHz) et "
                       "un fil HF, avec un commutateur pour garder la discone sur les autres modules.")).pack(
            anchor="w", pady=(4, 0))
        self._wfp_loaded = False

    # --------------------------------------------------------- Réglages
    def _wf_station_changed(self):
        name = self.wf_station.get()
        khz = next((k for n, k in STATIONS if n == name), None)
        if khz:
            self.wf_khz.set(f"{khz:g}".replace(".", ","))

    def _wf_get_khz(self):
        try:
            return float(self.wf_khz.get().replace(",", ".").strip())
        except ValueError:
            return None

    def _wf_update_dial(self):
        khz = self._wf_get_khz()
        self.wf_dial.config(text=f"→ radio sur {khz - 1.9:.1f} kHz en USB / DATA-U".replace(".", ",") if khz else "")

    def _wf_list_devices(self):
        try:
            import sounddevice as sd
            devs = [d["name"] for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]
        except Exception as exc:
            self.wf_info.config(text=f"Carte son inaccessible : {exc}")
            devs = []
        seen = []
        for d in devs:
            if d not in seen:
                seen.append(d)
        self.wf_dev_cb.config(values=seen)
        if not self.wf_dev.get() or self.wf_dev.get() not in seen:
            pick = next((d for d in seen if "USB Audio CODEC" in d or "CODEC" in d.upper()), seen[0] if seen else "")
            self.wf_dev.set(pick)

    def _wf_save_cfg(self):
        khz = self._wf_get_khz()
        self.config_data.update(wefax_station=self.wf_station.get(), wefax_audio=self.wf_dev.get(),
                                cat_port=self.wf_port.get().strip(), cat_baud=int(self.wf_baud.get() or 38400))
        if khz:
            self.config_data["wefax_khz"] = khz
        try:
            save_config(self.config_data)
        except Exception:
            pass

    def _wf_cat_tune(self):
        khz = self._wf_get_khz()
        if not khz:
            messagebox.showwarning("WEFAX", "Fréquence invalide.")
            return
        self._wf_save_cfg()
        hz = int(round((khz - 1.9) * 1000))
        port, baud = self.wf_port.get().strip(), int(self.wf_baud.get() or 38400)

        def worker():
            try:
                import serial
                with serial.Serial(port, baud, timeout=1, write_timeout=2) as ser:
                    ser.write(f"FA{hz:09d};".encode())
                    time.sleep(0.15)
                    ser.write(b"MD0C;")            # DATA-USB sur le FT-991 / 991A
                    time.sleep(0.15)
                    ser.write(b"FA;")
                    rep = ser.read(14).decode(errors="ignore")
                msg = f"Radio réglée sur {hz / 1000:.1f} kHz DATA-U".replace(".", ",") if f"{hz:09d}" in rep \
                    else "Commande envoyée (pas de confirmation de la radio : vérifiez port et vitesse)."
            except Exception as exc:
                msg = f"CAT impossible sur {port} : {exc} (VarAC ou WSJT-X ouvert ?)"
            self.after(0, lambda: self.wf_info.config(text=msg))

        threading.Thread(target=worker, daemon=True).start()

    # ----------------------------------------------------------- Écoute
    def _wf_toggle(self):
        if self._wf_stream:
            self._wf_stop()
        else:
            self._wf_start()

    def _wf_start(self):
        try:
            import sounddevice as sd
        except Exception as exc:
            messagebox.showwarning("WEFAX", f"Accès à la carte son impossible : {exc}")
            return
        self._wf_save_cfg()
        name = self.wf_dev.get()
        dev = None
        for i, d in enumerate(sd.query_devices()):
            if d["name"] == name and d.get("max_input_channels", 0) > 0:
                dev = i
                break
        rate = None
        for r in (11025, 12000, 16000, 22050, 44100, 48000):
            try:
                sd.check_input_settings(device=dev, samplerate=r, channels=1, dtype="float32")
                rate = r
                break
            except Exception:
                continue
        if not rate:
            messagebox.showwarning("WEFAX", "Cette entrée son n'accepte aucune fréquence d'échantillonnage utile.")
            return
        q = self._wf_q

        def cb(indata, frames, t, status):
            try:
                q.put_nowait(indata[:, 0].copy())
            except queue.Full:
                pass

        try:
            self._wf_stream = sd.InputStream(device=dev, samplerate=rate, channels=1, dtype="float32",
                                             blocksize=int(rate / 4), callback=cb)
            self._wf_stream.start()
        except Exception as exc:
            self._wf_stream = None
            messagebox.showwarning("WEFAX", f"Ouverture de l'entrée son impossible : {exc}")
            return
        ppm = float(self.config_data.get("wefax_ppm", 0.0))
        self._wf_rows = []
        self._wf_dec = wefax.WefaxDecoder(rate, ppm=ppm, on_start=lambda info: self.after(0, self._wf_on_start, info),
                                          on_line=self._wf_on_line,
                                          on_end=lambda img, info: self.after(0, self._wf_on_end, img, info))
        self._wf_run = True
        threading.Thread(target=self._wf_worker, daemon=True).start()
        self.wf_btn.config(text="■ Arrêter")
        self._wf_set_state("ÉCOUTE", theme.GREEN)
        self.after(1000, self._wf_tick)

    def _wf_stop(self):
        self._wf_run = False
        try:
            self._wf_stream.stop()
            self._wf_stream.close()
        except Exception:
            pass
        self._wf_stream = None
        if self._wf_dec:
            self._wf_dec.flush()
        self.wf_btn.config(text="▶ Écouter")
        self._wf_set_state("ARRÊTÉ", theme.MUTED)

    def _wf_worker(self):
        while self._wf_run:
            try:
                x = self._wf_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._wf_dec.feed(x * 20000)
            except Exception as exc:
                self.after(0, lambda e=exc: self.wf_info.config(text=f"Décodage : {e}"))

    def _wf_set_state(self, txt, color):
        self.wf_state.config(text=f"  {txt}  ", bg=color)

    def _wf_on_start(self, info):
        self._wf_rows = []
        self._wf_set_state("CARTE EN COURS", theme.ORANGE)
        self.wf_info.config(text="Début de carte détecté" + (" (tonalité de départ)" if info.get("header") else
                                                               " (prise en cours de route)"))

    def _wf_on_line(self, n, row):
        self._wf_rows.append(row)          # fil de décodage : simple ajout, l'affichage lit la liste

    def _wf_on_end(self, img, info):
        wefax_dir().mkdir(parents=True, exist_ok=True)
        khz = self._wf_get_khz() or 0
        name = time.strftime("%Y%m%d-%H%M") + f"_{khz:g}kHz".replace(".", ",")
        path = wefax_dir() / f"{name}.png"
        wefax.save_png(img, str(path))
        if info.get("ppm") is not None:
            self.config_data["wefax_ppm"] = float(info["ppm"])   # dérive de la carte son, pour la suite
            try:
                save_config(self.config_data)
            except Exception:
                pass
        self.wf_info.config(text=f"Carte enregistrée : {path.name} ({info.get('lines', 0)} lignes)")
        self._wf_send_to_pi(path, khz)
        self._wf_set_state("ÉCOUTE", theme.GREEN)
        self._wf_refresh_gallery()

    def _wf_send_to_pi(self, path, khz):
        """Copie la carte sur le Pi, qui la dépose sur GitHub (si activé) pour la
        comparer à l'original du DWD dans le bulletin du matin."""
        try:
            host, port = self._monitor_endpoint()
        except Exception:
            return
        name = time.strftime("%Y%m%d-%H%MZ", time.gmtime()) + f"_{khz:g}kHz".replace(".", ",")

        def worker():
            try:
                import base64
                requests.post(f"http://{host}:{port}/api/monitor/wefax", timeout=60,
                              json={"name": name, "png_b64": base64.b64encode(Path(path).read_bytes()).decode()})
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _wf_tick(self):
        if not self._wf_stream:
            return
        dec = self._wf_dec
        lvl = getattr(dec, "level", 0)
        st = {"idle": "attente d'une carte", "start": "tonalité de départ", "phasing": "phasage",
              "image": f"réception : {len(self._wf_rows)} lignes"}.get(dec.state, dec.state)
        if dec.state != "image" or not self.wf_info.cget("text").startswith("Carte enregistrée"):
            self.wf_info.config(text=f"{st} · signal {lvl * 100:.0f} %")
        self._wf_draw_live()
        self.after(1500, self._wf_tick)

    def _wf_draw_live(self):
        rows = self._wf_rows[-1600:]
        cv = self.wf_canvas
        cv.delete("all")
        if not rows:
            cv.create_text(cv.winfo_width() / 2, cv.winfo_height() / 2, fill=theme.MUTED, font=theme.FONT_BODY,
                           text="En attente d'une carte…")
            return
        img = Image.fromarray(np.array(rows, np.uint8), "L")
        cw, ch = max(200, cv.winfo_width()), max(200, cv.winfo_height())
        scale = cw / img.width
        img = img.resize((cw, max(1, int(img.height * scale))))
        if img.height > ch:
            img = img.crop((0, img.height - ch, cw, img.height))
        self._wf_photo = ImageTk.PhotoImage(img)
        cv.create_image(0, 0, anchor="nw", image=self._wf_photo)

    # ---------------------------------------------------------- Galerie
    def _wf_refresh_gallery(self):
        local = []
        d = wefax_dir()
        if d.is_dir():
            local = sorted(d.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[:9]
        base = self._mt_base()

        def worker():
            try:
                imgs = requests.get(base + "/api/meteor/images", timeout=6).json()
                pi = [i for i in imgs if i.get("kind") == "wefax"][:6]
                for it in pi:
                    try:
                        it["_thumb"] = requests.get(f"{base}/api/meteor/thumb/{it['name']}", timeout=8).content
                    except Exception:
                        it["_thumb"] = None
            except Exception:
                pi = []
            self.after(0, self._wf_draw_gallery, local, pi)

        threading.Thread(target=worker, daemon=True).start()

    def _wf_draw_gallery(self, local, pi):
        g = self.wf_gallery
        for w in g.winfo_children():
            w.destroy()
        self._wf_thumbs = []
        items = [("pc", p) for p in local] + [("pi", i) for i in pi]
        if not items:
            tk.Label(g, text="Aucune carte pour l'instant.", bg=theme.BG, fg=theme.MUTED,
                     font=theme.FONT_BODY).pack(anchor="w")
            return
        for k, (src, it) in enumerate(items[:12]):
            cell = tk.Frame(g, bg=theme.BG_CARD, cursor="hand2")
            cell.grid(row=k // 3, column=k % 3, padx=4, pady=4, sticky="n")
            try:
                if src == "pc":
                    im = Image.open(it).convert("L")
                    im.thumbnail((theme.px(170), theme.px(130)))
                    src_lbl = "DWD" if it.name.startswith("DWD_") else "PC"
                    label = f"{src_lbl} · {time.strftime('%d/%m %H:%M', time.localtime(it.stat().st_mtime))}"
                else:
                    import io
                    im = Image.open(io.BytesIO(it["_thumb"]))
                    im.thumbnail((theme.px(170), theme.px(130)))
                    label = f"Pi · {time.strftime('%d/%m %H:%M', time.localtime(it['aos']))}"
                ph = ImageTk.PhotoImage(im)
            except Exception:
                continue
            self._wf_thumbs.append(ph)
            l1 = tk.Label(cell, image=ph, bg=theme.BG_CARD)
            l1.pack()
            l2 = tk.Label(cell, text=label, bg=theme.BG_CARD, fg=theme.TEXT, font=("Segoe UI", theme.fs(8)))
            l2.pack()
            cmd = (lambda e, p=it: self._wf_open_local(p)) if src == "pc" else (lambda e, n=it["name"]: self._mt_open(n))
            for w in (cell, l1, l2):
                w.bind("<Button-1>", cmd)

    def _wf_open_local(self, path):
        try:
            base = np.asarray(Image.open(path).convert("L"))
        except Exception as exc:
            messagebox.showwarning("WEFAX", f"Image illisible : {exc}")
            return
        win = tk.Toplevel(self)
        win.title(f"WEFAX — {Path(path).name}")
        win.configure(bg=theme.BG)
        win.geometry(f"{theme.px(1200)}x{theme.px(860)}")
        bar = tk.Frame(win, bg=theme.BG)
        bar.pack(fill="x", padx=10, pady=6)
        tk.Label(bar, text="Redresser :", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_BODY).pack(side="left")
        slope = tk.DoubleVar(value=0.0)
        off = tk.IntVar(value=0)
        tk.Scale(bar, from_=-2.0, to=2.0, resolution=0.01, orient="horizontal", length=theme.px(260),
                 variable=slope, bg=theme.BG, fg=theme.TEXT, highlightthickness=0,
                 troughcolor=theme.BG_CARD_ALT).pack(side="left", padx=6)
        tk.Label(bar, text="Décaler :", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_BODY).pack(side="left", padx=(10, 0))
        tk.Scale(bar, from_=0, to=base.shape[1] - 1, orient="horizontal", length=theme.px(260), variable=off,
                 bg=theme.BG, fg=theme.TEXT, highlightthickness=0, troughcolor=theme.BG_CARD_ALT).pack(side="left",
                                                                                                     padx=6)
        cv = tk.Canvas(win, bg="#05070f", highlightthickness=0)
        cv.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        holder = {}

        def redraw(*_a):
            arr = shear(base, slope.get(), off.get())
            im = Image.fromarray(arr, "L")
            cw = max(300, cv.winfo_width())
            im = im.resize((cw, max(1, int(im.height * cw / im.width))))
            holder["ph"] = ImageTk.PhotoImage(im)
            cv.delete("all")
            cv.create_image(0, 0, anchor="nw", image=holder["ph"])
            cv.config(scrollregion=(0, 0, im.width, im.height))

        def save():
            arr = shear(base, slope.get(), off.get())
            Image.fromarray(arr, "L").save(path)
            if abs(slope.get()) > 0.001:
                # mémorise la correction pour les prochaines cartes
                self.config_data["wefax_ppm"] = float(self.config_data.get("wefax_ppm", 0.0)) + \
                    slope.get() / base.shape[1] * 1e6
                save_config(self.config_data)
            win.destroy()
            self._wf_refresh_gallery()

        def delete():
            if messagebox.askyesno("WEFAX", "Supprimer cette carte ?", parent=win):
                try:
                    Path(path).unlink()
                except OSError:
                    pass
                win.destroy()
                self._wf_refresh_gallery()

        ttk.Button(bar, text="Enregistrer", style="Accent.TButton", command=save).pack(side="left", padx=(12, 4))
        ttk.Button(bar, text="Supprimer", style="Flat.TButton", command=delete).pack(side="left")
        cv.bind("<MouseWheel>", lambda e: cv.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        cv.bind("<Configure>", redraw)
        slope.trace_add("write", lambda *_a: win.after_idle(redraw))
        off.trace_add("write", lambda *_a: win.after_idle(redraw))

    # --------------------------------------------------------------- Pi
    def _wfp_load(self, cfg):
        if self._wfp_loaded or "wefax_enabled" not in cfg:
            if "wefax_enabled" not in cfg:
                self.wfp_info.config(text="Mettez à jour le Pi pour la réception WEFAX automatique.")
            return
        self._wfp_loaded = True
        self.wfp_on.set(bool(cfg.get("wefax_enabled")))
        self.wfp_off.set(f"{cfg.get('wefax_offset_mhz', 125):g}")
        self.wfp_slots.delete("1.0", "end")
        for s in cfg.get("wefax_slots") or []:
            self.wfp_slots.insert("end", f"{s['time']}  {s['dur']}  {s['khz']:g}\n")

    def _wfp_apply(self):
        slots = []
        for line in self.wfp_slots.get("1.0", "end").splitlines():
            parts = line.replace(",", ".").split()
            if not parts:
                continue
            if len(parts) != 3:
                messagebox.showwarning("WEFAX", f"Ligne illisible : « {line} » (attendu : 10:40  20  7880)")
                return
            slots.append({"time": parts[0], "dur": parts[1], "khz": parts[2]})
        try:
            body = {"wefax_enabled": bool(self.wfp_on.get()),
                    "wefax_offset_mhz": float(self.wfp_off.get().replace(",", ".")), "wefax_slots": slots}
        except ValueError:
            messagebox.showwarning("WEFAX", "Décalage invalide.")
            return
        base = self._mt_base()

        def worker():
            try:
                r = requests.post(base + "/api/meteor/config", json=body, timeout=8)
                msg = "Créneaux enregistrés sur le Pi." if r.ok else f"Refusé : {r.json().get('error', r.status_code)}"
            except Exception as exc:
                msg = f"Pi injoignable : {exc}"
            self.after(0, lambda: self.wfp_info.config(text=msg))

        threading.Thread(target=worker, daemon=True).start()

    def _wf_start_poll(self):
        base = self._mt_base()

        def worker():
            try:
                cfg = requests.get(base + "/api/meteor/status", timeout=6).json().get("config") or {}
            except Exception:
                cfg = None
            if cfg is not None:
                self.after(0, self._wfp_load, cfg)

        threading.Thread(target=worker, daemon=True).start()
        self._wf_refresh_gallery()
