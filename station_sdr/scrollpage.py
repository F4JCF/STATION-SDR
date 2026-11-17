"""Pages défilables : si le contenu d'une page est plus haut que la fenêtre
(petit écran, page longue), une barre de défilement verticale apparaît et la
molette fait défiler la page. Quand tout tient, la page remplit la hauteur
disponible comme avant (cartes et tableaux s'étirent normalement)."""

import tkinter as tk
from tkinter import ttk


class ScrollPage:
    """`shell` se place dans la zone des pages ; `inner` reçoit le contenu."""

    def __init__(self, parent, bg):
        self.shell = tk.Frame(parent, bg=bg)
        self.canvas = tk.Canvas(self.shell, bg=bg, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self.shell, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.inner._scrollpage = self
        self._win = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)
        self._pending = False
        self.scrollable = False
        self.canvas.bind("<Configure>", self._schedule)
        self.inner.bind("<Configure>", self._schedule)

    def _schedule(self, _e=None):
        if not self._pending:
            self._pending = True
            self.canvas.after_idle(self._layout)

    def _layout(self):
        self._pending = False
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        need = self.inner.winfo_reqheight()
        scroll = need > ch + 2
        if scroll != self.scrollable:
            self.scrollable = scroll
            if scroll:
                self.vsb.pack(side="right", fill="y", before=self.canvas)
            else:
                self.vsb.pack_forget()
                self.canvas.yview_moveto(0)
            self.canvas.after_idle(self._schedule)
        h = need if scroll else ch
        self.canvas.itemconfigure(self._win, width=cw, height=h)
        self.canvas.configure(scrollregion=(0, 0, cw, h))

    def scroll(self, units):
        if self.scrollable:
            self.canvas.yview_scroll(units, "units")


def _owns_wheel(widget, shell_canvas):
    """Vrai si le widget sous la souris (ou un parent) gère déjà la molette."""
    w = widget
    while w is not None:
        if w is shell_canvas:
            return False
        cls = w.winfo_class()
        if cls in ("Treeview", "Text", "Listbox", "TCombobox", "Spinbox", "TSpinbox", "Scale", "TScale"):
            return True
        if cls == "Canvas":
            try:
                if str(w.cget("yscrollcommand")):
                    return True
            except tk.TclError:
                pass
            if type(w.master).__name__ == "TkinterMapView":
                return True
        w = getattr(w, "master", None)
    return False


def install_wheel(root, current_page):
    """Molette globale : fait défiler la page affichée (`current_page()` rend
    le ScrollPage visible ou None), sauf au-dessus d'un tableau, d'une carte,
    d'un champ de texte... qui gardent leur propre défilement."""

    def handler(event):
        sp = current_page()
        if sp is None or not sp.scrollable:
            return
        try:
            w = root.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        if w is None or w.winfo_toplevel() is not root or _owns_wheel(w, sp.canvas):
            return
        if getattr(event, "num", None) == 4:
            units = -3
        elif getattr(event, "num", None) == 5:
            units = 3
        else:
            units = -3 if event.delta > 0 else 3
        sp.scroll(units)

    root.bind_all("<MouseWheel>", handler, add="+")
    root.bind_all("<Button-4>", handler, add="+")
    root.bind_all("<Button-5>", handler, add="+")
