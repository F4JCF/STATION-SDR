"""Page « Satellites » : images LRPT des satellites Meteor-M et images SSTV
de l'ISS (événements ARISS), reçues automatiquement par le Pi pendant les
passages (service meteor-api)."""

import datetime
import io
import webbrowser
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import requests
from PIL import Image, ImageTk

from . import theme


class MeteorMixin:
    def _mt_base(self):
        return f"http://{self.config_data.get('pi_host', '192.168.1.229')}:{self.config_data.get('port_meteor', 5013)}"

    def _build_meteor_tab(self, frame):
        outer = tk.Frame(frame, bg=theme.BG)
        outer.pack(fill="both", expand=True, padx=4, pady=4)
        c = theme.card(outer)
        c.pack(fill="x", padx=12, pady=(8, 6))
        h = c.inner
        h.configure(padding=12)
        r = tk.Frame(h, bg=theme.BG_CARD)
        r.pack(fill="x")
        tk.Label(r, text="Images satellites : Meteor-M (137,9 MHz) et SSTV de l'ISS (145,800 MHz)", bg=theme.BG_CARD,
                 fg=theme.TEXT, font=theme.FONT_SECTION).pack(side="left")
        self.mt_pill = theme.pill(r, "INCONNU", theme.MUTED)
        self.mt_pill.pack(side="left", padx=(12, 0))
        self.mt_info = tk.Label(r, text="", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self.mt_info.pack(side="left", padx=12)
        ttk.Button(r, text="Bruit radio…", style="Flat.TButton", command=self._open_noise_window).pack(side="right")
        r2 = tk.Frame(h, bg=theme.BG_CARD)
        r2.pack(fill="x", pady=(8, 0))
        self.mt_enabled = tk.BooleanVar(value=True)
        tk.Checkbutton(r2, text="Meteor-M : réception automatique", variable=self.mt_enabled,
                       bg=theme.BG_CARD, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG_CARD,
                       activeforeground=theme.TEXT, font=theme.FONT_BODY, highlightthickness=0).pack(side="left")
        tk.Label(r2, text="Élévation minimale :", bg=theme.BG_CARD, fg=theme.MUTED,
                 font=theme.FONT_SUBTITLE).pack(side="left", padx=(16, 4))
        self.mt_minel = tk.StringVar(value="25")
        ttk.Combobox(r2, textvariable=self.mt_minel, width=4, values=("15", "20", "25", "30", "40", "50")).pack(side="left")
        tk.Label(r2, text="°   Gain :", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE).pack(side="left")
        self.mt_gain = tk.StringVar(value="40")
        ttk.Combobox(r2, textvariable=self.mt_gain, width=5, values=("30", "35", "40", "45", "49.6")).pack(
            side="left", padx=4)
        ttk.Button(r2, text="Appliquer", style="Flat.TButton", command=self._mt_apply).pack(side="left", padx=8)
        r3 = tk.Frame(h, bg=theme.BG_CARD)
        r3.pack(fill="x", pady=(6, 0))
        self.mt_sstv = tk.BooleanVar(value=False)
        tk.Checkbutton(r3, text="SSTV de l'ISS : toujours", variable=self.mt_sstv,
                       bg=theme.BG_CARD, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG_CARD,
                       activeforeground=theme.TEXT, font=theme.FONT_BODY, highlightthickness=0).pack(side="left")
        tk.Label(r3, text="ou jusqu'au (inclus) :", bg=theme.BG_CARD, fg=theme.MUTED,
                 font=theme.FONT_SUBTITLE).pack(side="left", padx=(12, 4))
        self.mt_sstv_until = tk.StringVar(value="")
        ttk.Entry(r3, textvariable=self.mt_sstv_until, width=11).pack(side="left")
        tk.Label(r3, text="(JJ/MM/AAAA)   Élévation min. ISS :", bg=theme.BG_CARD, fg=theme.MUTED,
                 font=theme.FONT_SUBTITLE).pack(side="left", padx=(4, 4))
        self.mt_sstv_minel = tk.StringVar(value="15")
        ttk.Combobox(r3, textvariable=self.mt_sstv_minel, width=4, values=("10", "15", "20", "30")).pack(side="left")
        tk.Label(r3, text="°", bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_SUBTITLE).pack(side="left")
        ttk.Button(r3, text="Calendrier ARISS", style="Flat.TButton",
                   command=lambda: webbrowser.open("https://www.ariss.org/")).pack(side="left", padx=8)
        tk.Label(h, text=("Pendant un passage retenu (environ 10 min), la clé SDR passe sur le satellite puis revient "
                          "seule au module précédent. La veille 121,5 MHz n'est jamais interrompue. L'ISS n'émet en "
                          "SSTV que pendant les événements ARISS annoncés : indiquez la date de fin de l'événement."),
                 bg=theme.BG_CARD, fg=theme.MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(6, 0))

        body = tk.Frame(outer, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        left = tk.Frame(body, bg=theme.BG, width=560)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="Prochains passages", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION).pack(
            anchor="w", pady=(0, 4))
        tb = theme.card(left)
        tb.pack(fill="both", expand=True)
        cols = ("sat", "aos", "dur", "el", "dir", "st")
        self.mt_tree = ttk.Treeview(tb.inner, columns=cols, show="headings")
        for cname, (t, w) in {"sat": ("Satellite", 120), "aos": ("Lever", 110), "dur": ("Durée", 60),
                              "el": ("Élév. max", 75), "dir": ("Sens", 60), "st": ("", 110)}.items():
            self.mt_tree.heading(cname, text=t)
            self.mt_tree.column(cname, width=w, anchor="center")
        self.mt_tree.tag_configure("ok", foreground=theme.GREEN)
        self.mt_tree.tag_configure("low", foreground=theme.MUTED)
        self.mt_tree.pack(fill="both", expand=True, padx=1, pady=1)

        right = tk.Frame(body, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        gh = tk.Frame(right, bg=theme.BG)
        gh.pack(fill="x", pady=(0, 4))
        tk.Label(gh, text="Images reçues (clic : ouvrir)", bg=theme.BG, fg=theme.TEXT,
                 font=theme.FONT_SECTION).pack(side="left")
        self.mt_filter = tk.StringVar(value="all")
        for val, txt in (("sstv", "SSTV ISS"), ("lrpt", "Meteor"), ("all", "Toutes")):
            tk.Radiobutton(gh, text=txt, value=val, variable=self.mt_filter, command=self._mt_draw_gallery,
                           bg=theme.BG, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG,
                           activeforeground=theme.TEXT, font=theme.FONT_BODY, highlightthickness=0).pack(side="right")
        gc = theme.card(right)
        gc.pack(fill="both", expand=True)
        self.mt_gallery = tk.Frame(gc.inner, bg=theme.BG_CARD)
        self.mt_gallery.pack(fill="both", expand=True, padx=8, pady=8)
        self._mt_thumbs = {}
        self._mt_images = []
        self._mt_conf_loaded = False
        self._mt_poll_running = False

    def _mt_start_poll(self):
        if not getattr(self, "_mt_poll_running", False):
            self._mt_poll_running = True
            self._mt_poll()

    def _mt_poll(self):
        if getattr(self, "_current_view", None) != "meteor":
            self._mt_poll_running = False
            return
        threading.Thread(target=self._mt_fetch, daemon=True).start()
        self.after(15000, self._mt_poll)

    def _mt_fetch(self):
        base = self._mt_base()
        try:
            st = requests.get(base + "/api/meteor/status", timeout=6).json()
            imgs = requests.get(base + "/api/meteor/images", timeout=6).json()
            for im in [i for i in imgs if i.get("kind") == "sstv"][:12] + [i for i in imgs if i.get("kind") != "sstv"][:12]:
                if im["name"] not in self._mt_thumbs:
                    r = requests.get(f"{base}/api/meteor/thumb/{im['name']}", timeout=15)
                    if r.status_code == 200:
                        self._mt_thumbs[im["name"]] = r.content
            err = None
        except Exception as exc:
            st = imgs = None
            err = str(exc)
        self.after(0, self._mt_render, st, imgs, err)

    def _mt_render(self, st, imgs, err):
        if not hasattr(self, "mt_tree"):
            return
        if err:
            self.mt_pill.config(text="  NON INSTALLÉ / INJOIGNABLE  ", bg=theme.MUTED)
            self.mt_info.config(text="Installez le module : Santé du Pi → « Modules du Pi… » → Images satellites.")
            return
        cfg = st.get("config") or {}
        if not self._mt_conf_loaded:
            self._mt_conf_loaded = True
            self.mt_enabled.set(bool(cfg.get("enabled", True)))
            self.mt_minel.set(f"{cfg.get('min_el', 25):g}")
            self.mt_gain.set(f"{cfg.get('gain', 40):g}")
            self.mt_sstv.set(bool(cfg.get("sstv_enabled")))
            until = cfg.get("sstv_until") or ""
            try:
                until = datetime.date.fromisoformat(until).strftime("%d/%m/%Y") if until else ""
            except ValueError:
                pass
            self.mt_sstv_until.set(until)
            self.mt_sstv_minel.set(f"{cfg.get('sstv_min_el', 15):g}")
            if "sstv_enabled" not in cfg:
                self.mt_info.config(text="Mettez à jour le Pi pour recevoir la SSTV de l'ISS.")
        cur = st.get("current")
        if cur:
            self.mt_pill.config(text="  RÉCEPTION EN COURS  ", bg=theme.ORANGE)
            self.mt_info.config(text=f"{cur['sat']} jusqu'à {time.strftime('%H:%M', time.localtime(cur['los']))}")
        else:
            on = cfg.get("enabled", True) or st.get("sstv_active")
            self.mt_pill.config(text=("  AUTOMATIQUE · SSTV ISS  " if st.get("sstv_active") else "  AUTOMATIQUE  ")
                                if on else "  DÉSACTIVÉ  ", bg=theme.GREEN if on else theme.MUTED)
            last = st.get("last")
            self.mt_info.config(text=(f"Dernier passage : {last['sat']} à "
                                      f"{time.strftime('%H:%M', time.localtime(last['aos']))} — {last.get('result')}")
                                if last else (f"⚠ {st['error']}" if st.get("error") else ""))
        tree = self.mt_tree
        tree.delete(*tree.get_children())
        minel = float(cfg.get("min_el", 25))

        def is_ok(p):
            if "wanted" in p:
                return p["wanted"]
            return p["max_el"] >= minel and cfg.get("enabled", True) and p.get("kind") != "sstv"

        for i, p in enumerate(st.get("passes") or []):
            ok = is_ok(p)
            if ok:
                why = "sera reçu"
            elif p.get("kind") == "sstv" and not st.get("sstv_active"):
                why = "hors ARISS"
            else:
                why = "trop bas" if p["max_el"] < (cfg.get("sstv_min_el", 15) if p.get("kind") == "sstv" else minel) \
                    else "désactivé"
            tree.insert("", "end", iid=str(i), tags=("ok",) if ok else ("low",), values=(
                "ISS (SSTV)" if p.get("kind") == "sstv" else p["sat"].replace("Meteor-M ", ""),
                ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")[time.localtime(p["aos"]).tm_wday]
                + time.strftime(" %H:%M", time.localtime(p["aos"])),
                f"{(p['los'] - p['aos']) / 60:.0f} min", f"{p['max_el']:.0f}°",
                "S → N" if p.get("northbound") else "N → S", why))
        nxt = next((p for p in st.get("passes") or [] if is_ok(p)), None)
        if "meteor" in self.tile_chips:
            self.tile_chips["meteor"].config(
                text=(f"{'ISS' if nxt.get('kind') == 'sstv' else 'Meteor'} "
                      f"{time.strftime('%H:%M', time.localtime(nxt['aos']))} ({nxt['max_el']:.0f}°)")
                if nxt else f"{len(imgs or [])} image(s)")
        self._mt_images = imgs or []
        self._mt_draw_gallery()

    def _mt_draw_gallery(self):
        g = self.mt_gallery
        for w in g.winfo_children():
            w.destroy()
        flt = self.mt_filter.get() if hasattr(self, "mt_filter") else "all"
        shown = [im for im in self._mt_images
                 if flt == "all" or (im.get("kind") == "sstv") == (flt == "sstv")][:12]
        if not shown:
            tk.Label(g, text="Pas encore d'image : elles arrivent après le premier passage retenu.",
                     bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY).pack(anchor="w")
            return
        self._mt_photo = []
        for i, im in enumerate(shown):
            cell = tk.Frame(g, bg=theme.BG_CARD_ALT, cursor="hand2")
            cell.grid(row=i // 6, column=i % 6, padx=4, pady=4, sticky="n")
            data = self._mt_thumbs.get(im["name"])
            if data:
                try:
                    pi = Image.open(io.BytesIO(data))
                    pi.thumbnail((170, 230))
                    ph = ImageTk.PhotoImage(pi)
                    self._mt_photo.append(ph)
                    lab = tk.Label(cell, image=ph, bg=theme.BG_CARD_ALT)
                    lab.pack()
                    lab.bind("<Button-1>", lambda _e, n=im["name"]: self._mt_open(n))
                except Exception:
                    pass
            if im.get("kind") == "sstv":
                when = im.get("rx_time") or im["aos"]
                txt = (f"ISS · {time.strftime('%d/%m %H:%M', time.localtime(when))}\n"
                       f"{im.get('mode', 'SSTV')} · {'complète' if im.get('complete') else 'partielle'}")
            else:
                txt = (f"{time.strftime('%d/%m %H:%M', time.localtime(im['aos']))}\n"
                       f"{im['sat'].replace('Meteor-M ', '')} · {im['max_el']:.0f}° · "
                       + (f"{im['useful_pct']} % reçu" if im.get("useful_pct") is not None else f"{im['lines']} l."))
            t = tk.Label(cell, text=txt,
                         bg=theme.BG_CARD_ALT, fg=theme.TEXT, font=("Segoe UI", 8), justify="center")
            t.pack(pady=(2, 4))
            t.bind("<Button-1>", lambda _e, n=im["name"]: self._mt_open(n))

    def _mt_open(self, name):
        base = self._mt_base()

        def worker():
            try:
                r = requests.get(f"{base}/api/meteor/image/{name}", timeout=60)
                r.raise_for_status()
                data, err = r.content, None
            except Exception as exc:
                data, err = None, str(exc)
            self.after(0, show, data, err)

        def show(data, err):
            if err:
                messagebox.showwarning("Satellites", f"Image indisponible : {err}")
                return
            win = tk.Toplevel(self)
            win.title(f"Satellites — {name}")
            win.configure(bg=theme.BG)
            win.geometry("900x900")
            bar = tk.Frame(win, bg=theme.BG)
            bar.pack(fill="x", padx=8, pady=6)

            def save():
                path = filedialog.asksaveasfilename(parent=win, defaultextension=".png", initialfile=f"{name}.png",
                                                    filetypes=[("Image PNG", "*.png")])
                if path:
                    with open(path, "wb") as f:
                        f.write(data)

            def delete():
                if messagebox.askyesno("Satellites", "Supprimer cette image du Pi ?", parent=win):
                    try:
                        requests.post(f"{base}/api/meteor/delete/{name}", timeout=10)
                    except Exception:
                        pass
                    self._mt_thumbs.pop(name, None)
                    win.destroy()
                    threading.Thread(target=self._mt_fetch, daemon=True).start()

            ttk.Button(bar, text="Enregistrer…", style="Accent.TButton", command=save).pack(side="left")
            ttk.Button(bar, text="Supprimer", style="Flat.TButton", command=delete).pack(side="left", padx=6)
            cv = tk.Canvas(win, bg="#000000", highlightthickness=0)
            ys = ttk.Scrollbar(win, orient="vertical", command=cv.yview)
            cv.configure(yscrollcommand=ys.set)
            ys.pack(side="right", fill="y")
            cv.pack(fill="both", expand=True)
            img = Image.open(io.BytesIO(data))

            def fit(_e=None):
                w = max(200, cv.winfo_width())
                scale = min(1.0, w / img.width)
                im2 = img.resize((int(img.width * scale), int(img.height * scale)))
                cv._ph = ImageTk.PhotoImage(im2)
                cv.delete("all")
                cv.create_image(0, 0, anchor="nw", image=cv._ph)
                cv.configure(scrollregion=(0, 0, im2.width, im2.height))
            cv.bind("<Configure>", fit)
            cv.bind_all("<MouseWheel>", lambda e: cv.yview_scroll(int(-e.delta / 120), "units"))

        threading.Thread(target=worker, daemon=True).start()

    def _mt_apply(self):
        try:
            cfg = {"enabled": bool(self.mt_enabled.get()), "min_el": float(self.mt_minel.get().replace(",", ".")),
                   "gain": float(self.mt_gain.get().replace(",", ".")), "sstv_enabled": bool(self.mt_sstv.get()),
                   "sstv_min_el": float(self.mt_sstv_minel.get().replace(",", "."))}
            until = self.mt_sstv_until.get().strip()
            if until:
                d = None
                for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
                    try:
                        d = datetime.datetime.strptime(until, fmt).date()
                        break
                    except ValueError:
                        continue
                if d is None:
                    raise ValueError("date")
                cfg["sstv_until"] = d.isoformat()
                self.mt_sstv_until.set(d.strftime("%d/%m/%Y"))
            else:
                cfg["sstv_until"] = ""
        except ValueError:
            messagebox.showwarning("Satellites", "Valeurs invalides (date au format JJ/MM/AAAA).")
            return
        base = self._mt_base()

        def worker():
            try:
                requests.post(base + "/api/meteor/config", json=cfg, timeout=8).raise_for_status()
                err = None
            except Exception as exc:
                err = str(exc)
            if err:
                self.after(0, lambda: messagebox.showwarning("Satellites", f"Réglage refusé : {err}"))
            self._mt_fetch()

        threading.Thread(target=worker, daemon=True).start()
