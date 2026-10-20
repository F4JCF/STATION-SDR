"""Palette et petits widgets réutilisables pour le thème sombre de STATION-SDR."""

import tkinter as tk
from tkinter import ttk

BG = "#0b0f17"
BG_CARD = "#141b26"
BG_CARD_ALT = "#1b2433"
BORDER = "#23304a"
ACCENT = "#ff7a1a"
ACCENT_DIM = "#8a4a1f"
TEXT = "#e6edf3"
MUTED = "#8b96a5"
GREEN = "#3fb950"
RED = "#f85149"
ORANGE = "#d29922"
BLUE = "#4aa3ff"

FONT_TITLE = ("Segoe UI", 18, "bold")
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
        background=BG_CARD, foreground=MUTED,
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


def card(parent, **kwargs) -> ttk.Frame:
    """Un conteneur avec le fond 'carte' et un léger liseré."""
    outer = tk.Frame(parent, bg=BORDER, **kwargs)
    inner = ttk.Frame(outer, style="Card.TFrame")
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    outer.inner = inner
    return outer


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
        parent, text=f" {text} ", bg=BG_CARD_ALT, fg=TEXT,
        font=FONT_SUBTITLE, padx=6, pady=3,
    )
