"""Mode « Mission / Exercice » : veille, goniométrie et journal horodaté,
puis compte rendu imprimable (HTML, à enregistrer en PDF depuis le
navigateur) avec carte, relèvements, détections et journal."""

import base64
import html
import os
import re
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import requests

from . import api_client, gonio, static_map, theme
from .config import save_config

STATION_HOME = (43.590541, 7.002072)
WATCH_CHOICES = {
    "121,375 MHz (exercice ADRASEC)": ("elt121", 121.375),
    "121,500 MHz (détresse aéronautique)": ("elt121", 121.5),
    "243,000 MHz (détresse militaire)": ("elt121", 243.0),
    "406 MHz (COSPAS-SARSAT)": ("beacon", None),
    "Aucune veille radio": (None, None),
}


def missions_dir() -> Path:
    docs = Path.home() / "Documents"
    return (docs if docs.is_dir() else Path.home()) / "STATION-SDR" / "Missions"


def _dur(sec):
    sec = int(max(0, sec))
    return f"{sec // 3600} h {sec % 3600 // 60:02d}" if sec >= 3600 else f"{sec // 60} min"


class MissionMixin:
    def _mission(self):
        return self.config_data.get("mission") or {}

    def _mission_save(self, m):
        self.config_data["mission"] = m
        try:
            save_config(self.config_data)
        except Exception:
            pass

    def _mission_log(self, source, text):
        """Ajoute une ligne au journal si une mission est en cours (appelé
        par les alertes, la goniométrie, la bascule de clé…)."""
        m = self._mission()
        if not m.get("active"):
            return
        m.setdefault("journal", []).append({"t": time.time(), "src": source, "text": text})
        self._mission_save(m)
        if hasattr(self, "ms_tree"):
            self.after(0, self._mission_fill)

    # ------------------------------------------------------------ Page --

    def _build_mission_tab(self, frame):
        outer = tk.Frame(frame, bg=theme.BG)
        outer.pack(fill="both", expand=True, padx=4, pady=4)
        left = tk.Frame(outer, bg=theme.BG, width=theme.px(700))
        left.pack(side="left", fill="y", pady=(0, 6))
        left.pack_propagate(False)
        c = theme.card(left)
        c.pack(fill="x", padx=12, pady=(8, 6))
        f = c.inner
        f.configure(padding=14)
        tk.Label(f, text="Mission / Exercice", bg=theme.BG_CARD, fg=theme.TEXT, font=theme.FONT_SECTION).grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.ms_vars = {k: tk.StringVar() for k in ("title", "kind", "place", "operator", "watch")}
        rows = (("title", "Intitulé :", None), ("kind", "Type :", ("Exercice", "Mission réelle")),
                ("place", "Secteur / lieu :", None), ("operator", "Opérateur :", None),
                ("watch", "Veille radio :", tuple(WATCH_CHOICES)))
        self.ms_inputs = []
        for i, (k, lab, values) in enumerate(rows, start=1):
            tk.Label(f, text=lab, bg=theme.BG_CARD, fg=theme.MUTED, font=theme.FONT_BODY).grid(
                row=i, column=0, sticky="w", pady=3)
            w = (ttk.Combobox(f, textvariable=self.ms_vars[k], values=values, state="readonly", width=40) if values
                 else ttk.Entry(f, textvariable=self.ms_vars[k], width=43))
            w.grid(row=i, column=1, sticky="w", pady=3, padx=(8, 0))
            self.ms_inputs.append(w)
        self.ms_vars["kind"].set("Exercice")
        self.ms_vars["operator"].set("F4JCF — ADRASEC 06")
        self.ms_vars["watch"].set("121,375 MHz (exercice ADRASEC)")
        b = tk.Frame(f, bg=theme.BG_CARD)
        b.grid(row=7, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.ms_start_btn = ttk.Button(b, text="▶ Démarrer", style="Accent.TButton", command=self._mission_start)
        self.ms_start_btn.pack(side="left")
        self.ms_end_btn = ttk.Button(b, text="■ Terminer et rédiger le compte rendu", style="Accent.TButton",
                                     command=self._mission_end)
        self.ms_end_btn.pack(side="left", padx=8)
        ttk.Button(b, text="Compte rendu provisoire", style="Flat.TButton",
                   command=lambda: self._mission_report(final=False)).pack(side="left")
        ttk.Button(b, text="Envoyer…", style="Flat.TButton",
                   command=self._mission_send_dialog).pack(side="left", padx=(8, 0))
        self.ms_state = tk.Label(left, text="", bg=theme.BG_CARD_ALT, fg=theme.TEXT, font=("Segoe UI", 14, "bold"),
                                 pady=8, wraplength=theme.px(660), justify="center")
        self.ms_state.pack(fill="x", padx=12, pady=(0, 6))
        q = tk.Frame(left, bg=theme.BG)
        q.pack(fill="x", padx=12)
        ttk.Button(q, text="Ouvrir la veille", style="Flat.TButton",
                   command=lambda: (self.show_page("beacon"), self._beacon_band_select("121"))).pack(side="left")
        ttk.Button(q, text="Ouvrir la goniométrie", style="Flat.TButton",
                   command=lambda: (self.show_page("beacon"), self._beacon_band_select("gonio"))).pack(
            side="left", padx=6)
        ttk.Button(q, text="Dossier des comptes rendus", style="Flat.TButton",
                   command=self._mission_open_dir).pack(side="left")
        tk.Label(left, text=(
            "Pendant la mission, le journal se remplit tout seul : détections de sirène, relèvements saisis "
            "en goniométrie, alertes, bascules de la clé. Ajoutez vos propres notes (contacts radio, consignes, "
            "positions des équipes). Le compte rendu s'ouvre dans le navigateur (« Imprimer » → "
            "« Enregistrer en PDF ») ; « Envoyer… » l'expédie par e-mail et/ou sur votre téléphone."),
            bg=theme.BG, fg=theme.MUTED, font=("Segoe UI", 9), justify="left", wraplength=theme.px(670)).pack(
            anchor="w", padx=12, pady=(10, 0))

        right = tk.Frame(outer, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True, pady=(8, 12), padx=(0, 12))
        tk.Label(right, text="Journal de la mission", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION).pack(
            anchor="w", pady=(0, 4))
        tb = theme.card(right)
        tb.pack(fill="both", expand=True)
        self.ms_tree = ttk.Treeview(tb.inner, columns=("t", "src", "text"), show="headings")
        for cname, (t, w, a) in {"t": ("Heure", 80, "center"), "src": ("Source", 130, "w"),
                                 "text": ("Événement", 800, "w")}.items():
            self.ms_tree.heading(cname, text=t)
            self.ms_tree.column(cname, width=w, anchor=a, stretch=(cname == "text"))
        vsb = ttk.Scrollbar(tb.inner, orient="vertical", command=self.ms_tree.yview)
        self.ms_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.ms_tree.pack(fill="both", expand=True, padx=1, pady=1)
        nr = tk.Frame(right, bg=theme.BG)
        nr.pack(fill="x", pady=(6, 0))
        tk.Label(nr, text="Note :", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_BODY).pack(side="left")
        self.ms_note = tk.StringVar()
        e = ttk.Entry(nr, textvariable=self.ms_note)
        e.pack(side="left", fill="x", expand=True, padx=6)
        e.bind("<Return>", lambda _e: self._mission_add_note())
        ttk.Button(nr, text="Ajouter au journal", style="Accent.TButton",
                   command=self._mission_add_note).pack(side="left")
        m = self._mission()
        if m.get("active"):
            for k in ("title", "kind", "place", "operator", "watch"):
                self.ms_vars[k].set(m.get(k) or self.ms_vars[k].get())
        self._mission_fill()
        self._mission_tick()

    def _mission_tick(self):
        if not hasattr(self, "ms_state"):
            return
        m = self._mission()
        if m.get("active"):
            exo = m.get("kind") == "Exercice"
            self.ms_state.config(text=(f"{'EXERCICE' if exo else 'MISSION'} EN COURS depuis "
                                       f"{time.strftime('%H:%M', time.localtime(m['start']))} "
                                       f"({_dur(time.time() - m['start'])})\n« {m.get('title')} »"),
                                 bg=theme.ORANGE if exo else theme.RED, fg="#0b0f17")
            self.ms_start_btn.state(["disabled"])
            self.ms_end_btn.state(["!disabled"])
            for w in self.ms_inputs:
                w.state(["disabled"])
        else:
            self.ms_state.config(text="Aucune mission en cours", bg=theme.BG_CARD_ALT, fg=theme.MUTED)
            self.ms_start_btn.state(["!disabled"])
            self.ms_end_btn.state(["disabled"])
            for w in self.ms_inputs:
                w.state(["!disabled"] if isinstance(w, ttk.Entry) else ["readonly"])
        if "mission" in self.tile_chips:
            self.tile_chips["mission"].config(
                text=(f"⚠ {'Exercice' if m.get('kind') == 'Exercice' else 'Mission'} en cours"
                      if m.get("active") else "aucune mission"))
        self.after(30000, self._mission_tick)

    def _mission_fill(self):
        tree = self.ms_tree
        tree.delete(*tree.get_children())
        for i, e in enumerate(self._mission().get("journal") or []):
            tree.insert("", "end", iid=str(i), values=(time.strftime("%H:%M:%S", time.localtime(e["t"])),
                                                       e.get("src", ""), e.get("text", "")))
        kids = tree.get_children()
        if kids:
            tree.see(kids[-1])

    def _mission_add_note(self):
        txt = self.ms_note.get().strip()
        if not txt:
            return
        if not self._mission().get("active"):
            messagebox.showinfo("Mission", "Démarrez d'abord une mission pour tenir son journal.")
            return
        self._mission_log("Note", txt)
        self.ms_note.set("")

    def _mission_start(self):
        title = self.ms_vars["title"].get().strip()
        if not title:
            messagebox.showwarning("Mission", "Donnez un intitulé à la mission (ex. « Exercice SATER Coursegoules »).")
            return
        watch = self.ms_vars["watch"].get()
        m = {k: self.ms_vars[k].get().strip() for k in ("title", "kind", "place", "operator", "watch")}
        m.update(active=True, start=time.time(), journal=[], gonio_start=len(self.config_data.get("gonio_bearings") or []))
        self._mission_save(m)
        self._mission_log("Mission", f"Début : {m['kind'].lower()} « {title} »" + (f", secteur {m['place']}" if m['place'] else ""))
        mode, freq = WATCH_CHOICES.get(watch, (None, None))
        if mode == "elt121" and freq:
            self.elt_freq_var.set(f"{freq:.3f}".replace(".", ","))
            self._elt_apply()
            self._mission_log("Veille", f"Veille radio sur {freq:.3f} MHz".replace(".", ","))
        elif mode == "beacon":
            self._mission_log("Veille", "Veille 406 MHz (COSPAS-SARSAT)")
        if mode:
            self._switch_sdr(mode)
        if self.config_data.get("gonio_bearings") and messagebox.askyesno(
                "Mission", "Effacer les relèvements de goniométrie précédents pour repartir de zéro ?"):
            self.config_data["gonio_bearings"] = []
            m["gonio_start"] = 0
            self._mission_save(m)
            if hasattr(self, "_gonio_fill"):
                self._gonio_fill()
        self._mission_tick()

    def _mission_end(self):
        m = self._mission()
        if not m.get("active"):
            return
        if not messagebox.askyesno("Mission", "Terminer la mission et rédiger le compte rendu ?"):
            return
        self._mission_log("Mission", "Fin de la mission")
        m = self._mission()
        m["active"] = False
        m["end"] = time.time()
        self._mission_save(m)
        arch = self.config_data.setdefault("missions_archive", [])
        arch.append({k: m.get(k) for k in ("title", "kind", "place", "start", "end")})
        del arch[:-20]
        self._mission_tick()
        self._mission_report(final=True, offer_send=True)

    def _mission_open_dir(self):
        d = missions_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(d))  # type: ignore[attr-defined]
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(d)])
        except Exception:
            messagebox.showinfo("Missions", str(d))

    # -------------------------------------------------- Compte rendu --

    def _mission_report(self, final=True, offer_send=False):
        m = dict(self._mission())
        if not m.get("start"):
            messagebox.showinfo("Mission", "Aucune mission à résumer.")
            return
        bearings = list(self.config_data.get("gonio_bearings") or [])[m.get("gonio_start", 0):]
        host, port = self._elt_endpoint() if hasattr(self, "_elt_endpoint") else (None, None)

        def worker():
            events = []
            try:
                for e in api_client.get_elt(host, port, "events") or []:
                    if e.get("start", 0) >= m["start"] - 60 and e.get("start", 0) <= (m.get("end") or time.time()):
                        events.append(e)
            except Exception:
                pass
            try:
                path = self._mission_write_report(m, bearings, events, final)
                err = None
            except Exception as exc:
                path, err = None, str(exc)
            self.after(0, done, path, err)

        def done(path, err):
            if err:
                messagebox.showerror("Compte rendu", f"Rédaction impossible : {err}")
                return
            webbrowser.open(Path(path).as_uri())
            if offer_send and messagebox.askyesno(
                    "Compte rendu", "Compte rendu rédigé.\n\nL'envoyer maintenant par e-mail ou sur le téléphone ?"):
                self._mission_send_dialog(path)

        threading.Thread(target=worker, daemon=True).start()

    def _mission_write_report(self, m, bearings, events, final):
        esc = html.escape
        start, end = m["start"], m.get("end") or time.time()
        STATION = self._station_coords() if hasattr(self, "_station_coords") else STATION_HOME
        where = self._profile_current()["name"] if hasattr(self, "_profile_current") else "Mougins"
        est = gonio.estimate([(b["lat"], b["lon"], b["az"]) for b in bearings]) if len(bearings) >= 2 else None
        # Carte
        pts = [STATION] + [(b["lat"], b["lon"]) for b in bearings]
        lines, markers, circles = [], [(STATION[0], STATION[1], "F4JCF", (22, 163, 74))], []
        colors = [(37, 99, 235), (22, 163, 74), (147, 51, 234), (8, 145, 178), (202, 138, 4), (219, 39, 119)]
        teams = {}
        length = float(self.config_data.get("gonio_line_km", 40))
        for b in bearings:
            col = teams.setdefault(b.get("team"), colors[len(teams) % len(colors)])
            endp = gonio.destination(b["lat"], b["lon"], b["az"], length)
            lines.append(((b["lat"], b["lon"]), endp, col + (220,)))
            markers.append((b["lat"], b["lon"], f"{b.get('team')} {b['az']:.0f}°", col))
        if est and est.get("lat") is not None:
            circles.append((gonio.circle(est["lat"], est["lon"], est["radius_km"]), (239, 68, 68, 255)))
            markers.append((est["lat"], est["lon"], "Zone probable", (185, 28, 28)))
            pts.append((est["lat"], est["lon"]))
        img_html = ""
        if len(pts) >= 2:
            png = static_map.render(pts, lines=lines, circles=circles, markers=markers,
                                    style=self.config_data.get("map_style", "osm"))
            if png:
                img_html = f'<img class="map" src="data:image/png;base64,{base64.b64encode(png).decode()}">'
        fmt = lambda t: time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(t))  # noqa: E731
        hm = lambda t: time.strftime("%H:%M:%S", time.localtime(t))  # noqa: E731
        zone = "Moins de deux relèvements : pas de zone calculée."
        if est and est.get("lat") is not None:
            d, brg = gonio.dist_bearing(STATION[0], STATION[1], est["lat"], est["lon"])
            zone = (f"<b>{est['lat']:.5f}, {est['lon']:.5f}</b> ({esc(gonio_dm(est['lat'], est['lon']))}) — "
                    f"rayon ≈ {est['radius_km']:.1f} km — qualité {esc(est['quality'])} — "
                    f"{d:.1f} km au {brg:.0f}° de la station F4JCF<br>"
                    f"<a href='https://www.geoportail.gouv.fr/carte?c={est['lon']:.5f},{est['lat']:.5f}&z=14'>"
                    f"Ouvrir sur Géoportail</a>")
        rows_b = "".join(
            f"<tr><td>{hm(b.get('time', 0))}</td><td>{esc(str(b.get('team')))}</td>"
            f"<td>{b['lat']:.5f}, {b['lon']:.5f}<br><small>{esc(gonio_dm(b['lat'], b['lon']))}</small></td>"
            f"<td>{b['az']:.0f}°</td><td>{b['az_in']:.0f}° {'magn.' if b.get('mag') else 'vrai'}</td></tr>"
            for b in bearings) or "<tr><td colspan=5>Aucun relèvement.</td></tr>"
        rows_e = "".join(
            f"<tr><td>{hm(e['start'])}</td><td>{hm(e['end']) if e.get('end') else 'en cours'}</td>"
            f"<td>{_dur((e.get('end') or end) - e['start'])}</td><td>{e.get('freq_mhz')} MHz</td>"
            f"<td>+{e.get('max_above_db')} dB</td><td>{e.get('sweeps_per_s')}/s</td></tr>"
            for e in sorted(events, key=lambda e: e["start"])) or "<tr><td colspan=6>Aucune détection.</td></tr>"
        rows_j = "".join(f"<tr><td>{hm(j['t'])}</td><td>{esc(j.get('src', ''))}</td><td>{esc(j.get('text', ''))}</td></tr>"
                         for j in m.get("journal") or [])
        kind = m.get("kind") or "Mission"
        summary = [f"{kind} « {m.get('title', '')} »" + (f" — {m['place']}" if m.get("place") else ""),
                   f"Du {fmt(start)} au {fmt(end) if m.get('end') else '(en cours)'} ({_dur(end - start)})",
                   f"Veille : {m.get('watch') or '—'}",
                   f"Détections de sirène : {len(events)} · relèvements : {len(bearings)}"]
        if est and est.get("lat") is not None:
            summary.append(f"Zone probable : {est['lat']:.5f}, {est['lon']:.5f} ({gonio_dm(est['lat'], est['lon'])}), "
                           f"rayon ≈ {est['radius_km']:.1f} km, qualité {est['quality']}")
            summary.append(f"https://www.geoportail.gouv.fr/carte?c={est['lon']:.5f},{est['lat']:.5f}&z=14")
        doc = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Compte rendu — {esc(m.get('title', ''))}</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;color:#111;max-width:1050px;margin:24px auto;padding:0 16px}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:17px;border-bottom:2px solid #ff7a1a;padding-bottom:3px;margin-top:26px}}
.sub{{color:#555}} .tag{{display:inline-block;padding:2px 10px;border-radius:10px;color:#fff;
background:{'#d97706' if kind == 'Exercice' else '#dc2626'};font-weight:600}}
table{{border-collapse:collapse;width:100%;font-size:13px}} td,th{{border:1px solid #ccc;padding:4px 6px;text-align:left;vertical-align:top}}
th{{background:#f3f4f6}} .map{{width:100%;border:1px solid #999;margin-top:8px}} .grid td{{border:none;padding:2px 12px 2px 0}}
@media print{{a{{color:#111}} h2{{page-break-after:avoid}}}}
</style></head><body>
<h1>Compte rendu — {esc(m.get('title', ''))}</h1>
<div class="sub"><span class="tag">{esc(kind.upper())}</span> &nbsp; {'' if final else '<b>(provisoire)</b> &nbsp;'}
ADRASEC 06 · station F4JCF-SDR-STATION ({esc(where)})</div>
<h2>Résumé</h2>
<table class="grid">
<tr><td>Secteur / lieu</td><td><b>{esc(m.get('place') or '—')}</b></td></tr>
<tr><td>Opérateur</td><td>{esc(m.get('operator') or '—')}</td></tr>
<tr><td>Début</td><td>{fmt(start)}</td></tr><tr><td>Fin</td><td>{fmt(end) if m.get('end') else 'en cours'}</td></tr>
<tr><td>Durée</td><td>{_dur(end - start)}</td></tr>
<tr><td>Veille radio</td><td>{esc(m.get('watch') or '—')}</td></tr>
<tr><td>Détections de sirène</td><td>{len(events)}</td></tr>
<tr><td>Relèvements</td><td>{len(bearings)}</td></tr>
</table>
<h2>Zone probable d'émission</h2><p>{zone}</p>{img_html}
<h2>Relèvements des équipes</h2>
<table><tr><th>Heure</th><th>Équipe</th><th>Position</th><th>Azimut vrai</th><th>Lu</th></tr>{rows_b}</table>
<h2>Détections de la station (veille)</h2>
<table><tr><th>Début</th><th>Fin</th><th>Durée</th><th>Fréquence</th><th>Niveau max</th><th>Balayage</th></tr>{rows_e}</table>
<h2>Journal</h2>
<table><tr><th>Heure</th><th>Source</th><th>Événement</th></tr>{rows_j}</table>
<p class="sub" style="margin-top:30px">Document produit par STATION-SDR le {fmt(time.time())}.
Coordonnées WGS84. Les azimuts lus au compas sont corrigés de la déclinaison magnétique.</p>
</body></html>"""
        d = missions_dir()
        d.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", m.get("title", "mission"))[:40].strip("_") or "mission"
        path = d / f"{time.strftime('%Y-%m-%d_%H%M', time.localtime(start))}_{slug}{'' if final else '_provisoire'}.html"
        path.write_text(doc, encoding="utf-8")
        self._ms_last_report = {"path": str(path), "summary": "\n".join(summary),
                                "subject": f"Compte rendu{'' if final else ' provisoire'} — {m.get('title', '')}"}
        return str(path)

    # --------------------------------------------------------- Envoi --

    def _mission_send_dialog(self, path=None):
        last = getattr(self, "_ms_last_report", None) or {}
        if path is None:
            path = last.get("path")
            if not path or not os.path.exists(path):
                files = sorted(missions_dir().glob("*.html"), key=lambda p: p.stat().st_mtime) \
                    if missions_dir().is_dir() else []
                path = str(files[-1]) if files else None
        if not path:
            messagebox.showinfo("Envoyer", "Aucun compte rendu à envoyer : rédigez-en un d'abord.")
            return
        win = tk.Toplevel(self)
        win.title("Envoyer le compte rendu")
        win.configure(bg=theme.BG)
        win.transient(self)
        f = tk.Frame(win, bg=theme.BG, padx=16, pady=12)
        f.pack(fill="both", expand=True)
        tk.Label(f, text="Compte rendu :", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_BODY).grid(row=0, column=0, sticky="w")
        path_var = tk.StringVar(value=path)
        tk.Label(f, textvariable=path_var, bg=theme.BG, fg=theme.TEXT, font=theme.FONT_BODY, wraplength=theme.px(460),
                 justify="left").grid(row=0, column=1, sticky="w", padx=8)

        def choose():
            p = filedialog.askopenfilename(parent=win, initialdir=str(missions_dir()),
                                           filetypes=[("Compte rendu HTML", "*.html")])
            if p:
                path_var.set(p)
        ttk.Button(f, text="Autre…", style="Flat.TButton", command=choose).grid(row=0, column=2)
        v_mail = tk.BooleanVar(value=True)
        v_main = tk.BooleanVar(value=True)
        v_ntfy = tk.BooleanVar(value=True)
        v_extra = tk.StringVar(value=self.config_data.get("mission_extra_to", ""))
        cb = dict(bg=theme.BG, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG,
                  activeforeground=theme.TEXT, font=theme.FONT_BODY, highlightthickness=0)
        tk.Checkbutton(f, text="Par e-mail (compte rendu en pièce jointe)", variable=v_mail, **cb).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(12, 0))
        main_lbl = tk.Checkbutton(f, text="au destinataire des notifications", variable=v_main, **cb)
        main_lbl.grid(row=2, column=0, columnspan=3, sticky="w", padx=(24, 0))
        tk.Label(f, text="et à :", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_BODY).grid(
            row=3, column=0, sticky="e", padx=(24, 0))
        ttk.Entry(f, textvariable=v_extra, width=52).grid(row=3, column=1, columnspan=2, sticky="w", padx=8)
        tk.Label(f, text="(adresses séparées par des virgules, ex. le PC ADRASEC)", bg=theme.BG, fg=theme.MUTED,
                 font=("Segoe UI", 8)).grid(row=4, column=1, columnspan=2, sticky="w", padx=8)
        tk.Checkbutton(f, text="Sur le téléphone (ntfy : résumé + fichier à ouvrir)", variable=v_ntfy, **cb).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
        tk.Label(f, text=("Le fichier passe par le serveur ntfy.sh (gardé quelques heures, accessible à qui connaît "
                          "votre sujet ntfy). Décochez pour une mission sensible : seul le résumé partira."),
                 bg=theme.BG, fg=theme.MUTED, font=("Segoe UI", 8), wraplength=theme.px(520), justify="left").grid(
            row=6, column=0, columnspan=3, sticky="w", padx=(24, 0))
        v_file = tk.BooleanVar(value=True)
        tk.Checkbutton(f, text="joindre le fichier", variable=v_file, **cb).grid(
            row=7, column=0, columnspan=3, sticky="w", padx=(24, 0))
        status = tk.Label(f, text="", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_BODY, wraplength=theme.px(520),
                          justify="left")
        status.grid(row=9, column=0, columnspan=3, sticky="w", pady=(10, 0))
        host, port = self._monitor_endpoint()

        def load_dest():
            try:
                cfg = requests.get(f"http://{host}:{port}/api/monitor/config", timeout=6).json()
                to = (cfg.get("email") or {}).get("to") or "non configuré"
                topic = (cfg.get("ntfy") or {}).get("topic")
            except Exception:
                to, topic = "Pi injoignable", None
            self.after(0, lambda: (main_lbl.config(text=f"au destinataire des notifications ({to})"),
                                   None if topic else status.config(
                                       text="ntfy non configuré : Santé du Pi → Notifications.")))
        threading.Thread(target=load_dest, daemon=True).start()

        def send():
            p = path_var.get()
            try:
                doc = Path(p).read_text(encoding="utf-8")
            except OSError as exc:
                status.config(text=f"Lecture impossible : {exc}", fg=theme.RED)
                return
            if not (v_mail.get() or v_ntfy.get()):
                status.config(text="Choisissez au moins un moyen d'envoi.", fg=theme.ORANGE)
                return
            same = last.get("path") == p
            m = self._mission()
            subject = last.get("subject") if same else f"Compte rendu — {Path(p).stem}"
            summary = last.get("summary") if same else (
                f"{m.get('kind', 'Mission')} « {m.get('title', '')} »" if m.get("title") else Path(p).stem)
            self.config_data["mission_extra_to"] = v_extra.get().strip()
            self._mission_save(self._mission())
            payload = {"subject": subject, "summary": summary, "html": doc, "filename": Path(p).name,
                       "email": v_mail.get(), "email_main": v_main.get(), "extra_to": v_extra.get(),
                       "ntfy": v_ntfy.get(), "ntfy_file": v_file.get()}
            status.config(text="Envoi en cours…", fg=theme.MUTED)
            send_btn.state(["disabled"])

            def worker():
                try:
                    r = requests.post(f"http://{host}:{port}/api/monitor/send_document", json=payload, timeout=150)
                    if r.status_code == 404:
                        raise RuntimeError("le Pi ne connaît pas encore cet envoi : « Mettre à jour le Pi » d'abord")
                    r.raise_for_status()
                    ch = r.json().get("channels") or {}
                    txt = "\n".join(f"{k} : {v}" for k, v in ch.items()) or "rien envoyé"
                    ok = all(not str(v).startswith("échec") for v in ch.values()) and ch
                except Exception as exc:
                    txt, ok = f"Échec : {exc}", False
                self.after(0, finish, txt, ok)

            def finish(txt, ok):
                if not win.winfo_exists():
                    return
                status.config(text=txt, fg=theme.GREEN if ok else theme.RED)
                send_btn.state(["!disabled"])
                if ok:
                    self._mission_log("Envoi", f"Compte rendu envoyé ({txt.replace(chr(10), ' ; ')})")
            threading.Thread(target=worker, daemon=True).start()

        bb = tk.Frame(f, bg=theme.BG)
        bb.grid(row=10, column=0, columnspan=3, sticky="e", pady=(12, 0))
        send_btn = ttk.Button(bb, text="Envoyer", style="Accent.TButton", command=send)
        send_btn.pack(side="left")
        ttk.Button(bb, text="Fermer", style="Flat.TButton", command=win.destroy).pack(side="left", padx=6)


def gonio_dm(lat, lon):
    from .sater_ui import fmt_dm
    return fmt_dm(lat, lon)
