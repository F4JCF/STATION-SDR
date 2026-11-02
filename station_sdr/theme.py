"""Palette et petits widgets réutilisables pour le thème sombre de STATION-SDR."""

import tkinter as tk
from tkinter import ttk

# Thème « spatial » : bleu nuit profond, cartes bleutées à liseré lumineux,
# accent orange pour le module qui détient la clé SDR.
BG = "#040b1d"
BG_CARD = "#0a1838"
BG_CARD_ALT = "#10224d"
BORDER = "#2f6fe8"
GLOW = "#12306e"
ACCENT = "#ff8a1f"
ACCENT_DIM = "#8a4a1f"
TEXT = "#eef4ff"
MUTED = "#a9bbdd"
SUBTEXT = "#d3def5"
CYAN = "#29c4ff"
CHIP_BG = "#1c57d6"
CHIP_FG = "#ffffff"
GREEN = "#3fdc5a"
RED = "#f5433d"
ORANGE = "#ffb020"
BLUE = "#4aa3ff"

FONT_TITLE = ("Segoe UI", 18, "bold")
FONT_HERO = ("Segoe UI", 34, "bold italic")
FONT_CLOCK = ("Segoe UI", 34, "bold")
FONT_TILE = ("Segoe UI", 13, "bold")
FONT_SUBTITLE = ("Segoe UI", 10)
FONT_SECTION = ("Segoe UI", 12, "bold")
FONT_BODY = ("Segoe UI", 10)
FONT_BODY_BOLD = ("Segoe UI", 10, "bold")
FONT_MONO = ("Consolas", 9)
FONT_CHIP = ("Segoe UI", 9, "bold")


def apply_dark_theme(root: tk.Tk) -> ttk.Style:
    root.configure(bg=BG)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=BG_CARD)
    style.configure("TLabel", background=BG, foreground=TEXT, font=FONT_BODY)
    style.configure("Card.TLabel", background=BG_CARD, foreground=TEXT, font=FONT_BODY)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=FONT_SUBTITLE)
    style.configure("CardMuted.TLabel", background=BG_CARD, foreground=MUTED, font=FONT_SUBTITLE)
    style.configure("Title.TLabel", background=BG, foreground=TEXT, font=FONT_TITLE)
    style.configure("Section.TLabel", background=BG_CARD, foreground=ACCENT, font=FONT_SECTION)

    style.configure(
        "TNotebook", background=BG, borderwidth=0,
    )
    style.configure(
        "TNotebook.Tab",
        background=BG_CARD_ALT, foreground=SUBTEXT,
        padding=(16, 8), font=FONT_BODY_BOLD, borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", ACCENT)],
        foreground=[("selected", "#1a1200")],
    )

    style.configure(
        "Treeview",
        background=BG_CARD, fieldbackground=BG_CARD, foreground=TEXT,
        rowheight=26, borderwidth=0, font=FONT_BODY,
    )
    style.configure(
        "Treeview.Heading",
        background=BG_CARD_ALT, foreground=MUTED, font=FONT_BODY_BOLD, borderwidth=0,
    )
    style.map("Treeview", background=[("selected", ACCENT_DIM)])

    style.configure(
        "TEntry",
        fieldbackground=BG_CARD_ALT, foreground=TEXT, insertcolor=TEXT,
        bordercolor=BORDER, borderwidth=1, padding=4,
    )

    style.configure(
        "Accent.TButton",
        background=ACCENT, foreground="#1a1200", font=FONT_BODY_BOLD,
        padding=(10, 5), borderwidth=0,
    )
    style.map("Accent.TButton", background=[("active", "#ff8f3d")])

    style.configure(
        "Flat.TButton",
        background=BG_CARD_ALT, foreground=TEXT, font=FONT_BODY,
        padding=(8, 4), borderwidth=0,
    )
    style.map("Flat.TButton", background=[("active", BORDER)])

    return style


def _mix(c1: str, c2: str, t: float) -> str:
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def card(parent, **kwargs) -> tk.Frame:
    """Un conteneur « carte » avec un double liseré qui imite un halo
    lumineux : anneau extérieur sombre, filet intérieur bleu vif."""
    outer = tk.Frame(parent, bg=GLOW, **kwargs)
    mid = tk.Frame(outer, bg=BORDER)
    mid.pack(fill="both", expand=True, padx=1, pady=1)
    inner = ttk.Frame(mid, style="Card.TFrame")
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    outer.mid = mid
    outer.inner = inner
    return outer


def set_border(frame, color: str) -> None:
    """Change la couleur du liseré d'une carte (et de son halo)."""
    mid = getattr(frame, "mid", None)
    if mid is None:
        frame.config(bg=color)
        return
    mid.config(bg=color)
    frame.config(bg=GLOW if color == BORDER else _mix(color, BG, 0.55))


def status_dot(parent, color: str, size: int = 10) -> tk.Canvas:
    c = tk.Canvas(parent, width=size, height=size, bg=BG_CARD, highlightthickness=0)
    pad = 1
    c.create_oval(pad, pad, size - pad, size - pad, fill=color, outline="")
    return c


def pill(parent, text: str, color: str) -> tk.Label:
    return tk.Label(
        parent, text=f"  {text}  ", bg=color, fg="#0b0f17",
        font=FONT_CHIP, padx=2, pady=2,
    )


def chip(parent, text: str) -> tk.Label:
    return tk.Label(
        parent, text=f" {text} ", bg=CHIP_BG, fg=CHIP_FG,
        font=FONT_CHIP, padx=8, pady=4,
    )
