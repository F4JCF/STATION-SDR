"""Fenêtre « Messages APRS » : messagerie APRS texte par Internet (APRS-IS).
Le client tourne en arrière-plan tant que l'appli est ouverte (si activé)
et signale chaque message reçu."""

import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from . import aprs_is, plat, theme
from .config import save_config


class AprsMsgMixin:
    def _aprsmsg_call(self):
        return (self.config_data.get("aprsmsg_call") or "F4JCF").upper()

    def _aprsmsg_init(self):
        """Démarre le client au lancement de l'appli si l'utilisateur l'a activé."""
        self._aprsmsg = None
        if self.config_data.get("aprsmsg_enabled"):
            self._aprsmsg_start()

    def _aprsmsg_start(self):
        if getattr(self, "_aprsmsg", None):
            self._aprsmsg.stop()
        store = str(plat.config_dir() / "aprs_messages.json")
        self._aprsmsg = aprs_is.AprsMessenger(self._aprsmsg_call(), store,
                                              on_event=lambda k, d: self.after(0, self._aprsmsg_event, k, d))
        self._aprsmsg.start()

    def _aprsmsg_event(self, kind, data):
        if kind == "message":
            self._raise_alert(f"aprsmsg:{data['peer']}:{data.get('id') or data['t']}",
                              f"Message APRS de {data['peer']}", data["text"], cooldown=10 ** 9)
        self._aprsmsg_chip()
        win = getattr(self, "_am_win", None)
        if win is not None and win.winfo_exists():
            if kind == "status":
                self._am_status.config(text=f"APRS-IS : {data['text']}",
                                       fg=theme.GREEN if data["connected"] else theme.ORANGE)
            self._am_fill_convs()
            if kind in ("message", "ack") and data.get("peer") == self._am_peer:
                self._am_fill_thread()

    def _aprsmsg_chip(self):
        n = self._aprsmsg.unread() if getattr(self, "_aprsmsg", None) else 0
        if hasattr(self, "aprsmsg_btn"):
            self.aprsmsg_btn.config(text=f"Messages APRS ({n} non lu{'s' if n > 1 else ''})" if n else "Messages APRS…")

    def _open_aprsmsg(self):
        win = getattr(self, "_am_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            return
        win = tk.Toplevel(self)
        self._am_win = win
        win.title("Messages APRS (par Internet, APRS-IS)")
        win.configure(bg=theme.BG)
        win.geometry("980x620")
        top = tk.Frame(win, bg=theme.BG)
        top.pack(fill="x", padx=12, pady=8)
        tk.Label(top, text="Mon indicatif :", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_BODY).pack(side="left")
        self._am_call = tk.StringVar(value=self._aprsmsg_call())
        ttk.Entry(top, textvariable=self._am_call, width=11).pack(side="left", padx=4)
        self._am_on = tk.BooleanVar(value=bool(self.config_data.get("aprsmsg_enabled")))
        tk.Checkbutton(top, text="Connecté en permanence (au démarrage de l'appli)", variable=self._am_on,
                       bg=theme.BG, fg=theme.TEXT, selectcolor=theme.BG_CARD_ALT, activebackground=theme.BG,
                       activeforeground=theme.TEXT, font=theme.FONT_BODY, highlightthickness=0).pack(side="left", padx=8)
        ttk.Button(top, text="Appliquer", style="Flat.TButton", command=self._am_apply).pack(side="left")
        st = self._aprsmsg.status if getattr(self, "_aprsmsg", None) else "arrêté"
        ok = bool(getattr(self, "_aprsmsg", None) and self._aprsmsg.connected)
        self._am_status = tk.Label(top, text=f"APRS-IS : {st}", bg=theme.BG,
                                   fg=theme.GREEN if ok else theme.MUTED, font=theme.FONT_SUBTITLE)
        self._am_status.pack(side="left", padx=12)

        body = tk.Frame(win, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        left = tk.Frame(body, bg=theme.BG, width=theme.px(250))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        lb = tk.Frame(left, bg=theme.BG)
        lb.pack(fill="x", pady=(0, 4))
        tk.Label(lb, text="Conversations", bg=theme.BG, fg=theme.TEXT, font=theme.FONT_SECTION).pack(side="left")
        ttk.Button(lb, text="+ Nouveau", style="Flat.TButton", command=self._am_new).pack(side="right")
        self._am_tree = ttk.Treeview(left, columns=("peer", "last", "n"), show="headings")
        for c, (t, w) in {"peer": ("Indicatif", 100), "last": ("Dernier", 90), "n": ("", 40)}.items():
            self._am_tree.heading(c, text=t)
            self._am_tree.column(c, width=w, anchor="center")
        self._am_tree.tag_configure("unread", foreground=theme.ORANGE)
        self._am_tree.pack(fill="both", expand=True)
        self._am_tree.bind("<<TreeviewSelect>>", self._am_select)

        right = tk.Frame(body, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self._am_title = tk.Label(right, text="Choisissez ou commencez une conversation", bg=theme.BG,
                                  fg=theme.TEXT, font=theme.FONT_SECTION)
        self._am_title.pack(anchor="w", pady=(0, 4))
        self._am_text = tk.Text(right, bg=theme.BG_CARD_ALT, fg=theme.TEXT, font=("Segoe UI", theme.fs(10)), relief="flat",
                                wrap="word", padx=8, pady=6, state="disabled")
        self._am_text.tag_configure("in", foreground="#e5e7eb")
        self._am_text.tag_configure("out", foreground="#93c5fd", justify="right")
        self._am_text.tag_configure("meta", foreground=theme.MUTED, font=("Segoe UI", theme.fs(8)))
        self._am_text.tag_configure("meta_out", foreground=theme.MUTED, font=("Segoe UI", theme.fs(8)), justify="right")
        self._am_text.pack(fill="both", expand=True)
        sr = tk.Frame(right, bg=theme.BG)
        sr.pack(fill="x", pady=(6, 0))
        self._am_msg = tk.StringVar()
        self._am_msg.trace_add("write", lambda *_a: self._am_count.config(text=f"{len(self._am_msg.get())}/67"))
        e = ttk.Entry(sr, textvariable=self._am_msg)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda _e: self._am_send())
        self._am_count = tk.Label(sr, text="0/67", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SUBTITLE)
        self._am_count.pack(side="left", padx=6)
        ttk.Button(sr, text="Envoyer", style="Accent.TButton", command=self._am_send).pack(side="left")
        tk.Label(win, bg=theme.BG, fg=theme.MUTED, font=("Segoe UI", theme.fs(8)), justify="left", wraplength=theme.px(940),
                 text=("Les messages passent par Internet (réseau APRS-IS) : aucune émission radio. Ils atteignent "
                       "aussi les stations radio via les iGates qui relaient vers la VHF. 67 caractères au plus, "
                       "sans accents. Un message non acquitté est réémis 4 fois (30 s, 1, 2 puis 4 min).")).pack(
            anchor="w", padx=12, pady=(0, 8))
        self._am_peer = None
        self._am_fill_convs()

    def _am_apply(self):
        call = self._am_call.get().strip().upper()
        if not aprs_is.valid_call(call):
            messagebox.showwarning("Messages APRS", "Indicatif invalide (ex. F4JCF ou F4JCF-7).", parent=self._am_win)
            return
        if call.split("-")[0] != "F4JCF" and not messagebox.askyesno(
                "Messages APRS", f"« {call} » n'est pas votre indicatif. Continuer ?", parent=self._am_win):
            return
        if call in ("F4JCF-10", "F4JCF-1"):
            messagebox.showwarning("Messages APRS", f"{call} est déjà utilisé par un iGate : choisissez un autre "
                                   "SSID (ex. F4JCF ou F4JCF-7).", parent=self._am_win)
            return
        self.config_data["aprsmsg_call"] = call
        self.config_data["aprsmsg_enabled"] = bool(self._am_on.get())
        save_config(self.config_data)
        if self._am_on.get():
            self._aprsmsg_start()
        elif getattr(self, "_aprsmsg", None):
            self._aprsmsg.stop()
            self._am_status.config(text="APRS-IS : arrêté", fg=theme.MUTED)

    def _am_ensure(self):
        if not getattr(self, "_aprsmsg", None) or not self._aprsmsg.connected:
            if not getattr(self, "_aprsmsg", None):
                self._aprsmsg_start()
            return False
        return True

    def _am_fill_convs(self):
        tree = getattr(self, "_am_tree", None)
        if tree is None or not tree.winfo_exists():
            return
        sel = self._am_peer
        tree.delete(*tree.get_children())
        convs = self._aprsmsg.conversations() if getattr(self, "_aprsmsg", None) else []
        for c in convs:
            tree.insert("", "end", iid=c["peer"], tags=("unread",) if c["unread"] else (), values=(
                c["peer"], time.strftime("%d/%m %H:%M", time.localtime(c["last"])), c["unread"] or ""))
        if sel and tree.exists(sel):
            tree.selection_set(sel)

    def _am_select(self, _e=None):
        sel = self._am_tree.selection()
        if not sel or sel[0] == self._am_peer:
            return
        self._am_peer = sel[0]
        self._am_fill_thread()

    def _am_fill_thread(self):
        peer = self._am_peer
        if not peer or not getattr(self, "_aprsmsg", None):
            return
        self._aprsmsg.mark_read(peer)
        self._aprsmsg_chip()
        self._am_title.config(text=f"Conversation avec {peer}")
        t = self._am_text
        t.config(state="normal")
        t.delete("1.0", "end")
        for e in self._aprsmsg.thread(peer):
            when = time.strftime("%d/%m %H:%M", time.localtime(e["t"]))
            if e["dir"] == "in":
                to = f" → {e['to']}" if e.get("to") and e["to"] != self._aprsmsg.call else ""
                t.insert("end", f"{peer}{to} · {when}\n", "meta")
                t.insert("end", e["text"] + "\n\n", "in")
            else:
                t.insert("end", f"{when} · {e.get('state', '')}\n", "meta_out")
                t.insert("end", e["text"] + "\n\n", "out")
        t.see("end")
        t.config(state="disabled")
        self._am_fill_convs()

    def _am_new(self):
        peer = simpledialog.askstring("Nouveau message", "Indicatif du destinataire (ex. F1ABC-9) :",
                                      parent=self._am_win)
        if not peer:
            return
        peer = peer.strip().upper()
        if not aprs_is.valid_call(peer):
            messagebox.showwarning("Messages APRS", "Indicatif invalide.", parent=self._am_win)
            return
        self._am_peer = peer
        self._am_title.config(text=f"Conversation avec {peer}")
        self._am_fill_thread()

    def _am_send(self):
        text = self._am_msg.get().strip()
        if not text or not self._am_peer:
            if not self._am_peer:
                messagebox.showinfo("Messages APRS", "Choisissez d'abord un destinataire (« + Nouveau »).",
                                    parent=self._am_win)
            return
        if not self._am_ensure():
            messagebox.showwarning("Messages APRS", "Pas encore connecté à APRS-IS : réessayez dans quelques "
                                   "secondes.", parent=self._am_win)
            return
        if len(text) > 67:
            messagebox.showwarning("Messages APRS", "67 caractères au plus.", parent=self._am_win)
            return
        try:
            self._aprsmsg.send(self._am_peer, text)
        except ValueError as exc:
            messagebox.showwarning("Messages APRS", str(exc), parent=self._am_win)
            return
        self._am_msg.set("")
        self._am_fill_thread()
