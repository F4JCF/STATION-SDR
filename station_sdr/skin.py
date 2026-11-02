"""Décor graphique du thème « spatial » de STATION-SDR.

Tout est dessiné à la volée avec Pillow (aucune image lourde à embarquer) :
- le bandeau d'en-tête : ciel étoilé, horizon terrestre lumineux, spectre
  radio, satellite et cadre de l'horloge ;
- le pied de page : montagnes, lumières de la ville et pylône d'antenne.
Les images sont régénérées quand la fenêtre change de largeur.
"""

import math
import random

from PIL import Image, ImageDraw, ImageFilter

from . import theme


def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _vgradient(w, h, top, bottom):
    img = Image.new("RGB", (w, h), top)
    d = ImageDraw.Draw(img)
    t, b = _rgb(top), _rgb(bottom)
    for y in range(h):
        f = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(round(t[i] + (b[i] - t[i]) * f) for i in range(3)))
    return img


def _glow_layer(size, draw_fn, blur):
    """Calque RGBA flouté (halo) superposé à l'image."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer))
    return layer.filter(ImageFilter.GaussianBlur(blur))


def _stars(d, w, h, rng, count):
    for _ in range(count):
        x, y = rng.uniform(0, w), rng.uniform(0, h)
        a = rng.randint(70, 230)
        r = rng.choice((0.6, 0.8, 1.0, 1.4))
        d.ellipse([x - r, y - r, x + r, y + r], fill=(220, 235, 255, a))


def _satellite(img, cx, cy, s):
    """Petit satellite stylisé : corps cylindrique et deux panneaux."""
    d = ImageDraw.Draw(img)
    ang = math.radians(-18)
    ca, sa = math.cos(ang), math.sin(ang)

    def P(x, y):
        return (cx + (x * ca - y * sa) * s, cy + (x * sa + y * ca) * s)

    def quad(x0, y0, x1, y1):
        return [P(x0, y0), P(x1, y0), P(x1, y1), P(x0, y1)]

    for side in (-1, 1):  # panneaux solaires
        x0, x1 = (side * 0.75, side * 3.1) if side > 0 else (side * 3.1, side * 0.75)
        d.polygon(quad(x0, -0.42, x1, 0.42), fill=(28, 70, 170, 255), outline=(120, 180, 255, 255))
        for k in range(1, 6):
            xx = x0 + (x1 - x0) * k / 6
            d.line([P(xx, -0.42), P(xx, 0.42)], fill=(90, 150, 240, 255), width=1)
        d.line([P(x0, 0), P(x1, 0)], fill=(90, 150, 240, 255), width=1)
        d.line([P(side * 0.45, 0), P(side * 0.75, 0)], fill=(200, 210, 225, 255), width=2)
    d.polygon(quad(-0.5, -0.55, 0.5, 0.55), fill=(205, 212, 225, 255), outline=(240, 245, 255, 255))
    d.polygon(quad(-0.5, -0.12, 0.5, 0.12), fill=(170, 140, 70, 255))
    d.ellipse([P(0.55, -0.2)[0] - 0.25 * s, P(0.55, -0.2)[1] - 0.25 * s,
               P(0.55, -0.2)[0] + 0.25 * s, P(0.55, -0.2)[1] + 0.25 * s], fill=(255, 255, 255, 255))


def header_image(w, h, clock_box):
    """Fond du bandeau d'en-tête. `clock_box` = (x0, y0, x1, y1) du cadre
    de l'horloge, dessiné ici avec son halo."""
    w, h = max(200, int(w)), max(80, int(h))
    rng = random.Random(4)  # décor stable d'un affichage à l'autre
    img = _vgradient(w, h, "#020615", "#06184a").convert("RGBA")
    d = ImageDraw.Draw(img)
    _stars(d, w, h, rng, int(w * h / 900))

    # Horizon terrestre : grand arc lumineux à droite
    ex0, ey0, ex1, ey1 = w * 0.30, h * 0.70, w * 1.60, h * 3.6

    def earth_glow(gd):
        gd.ellipse([ex0, ey0, ex1, ey1], outline=(80, 170, 255, 255), width=10)

    img.alpha_composite(_glow_layer(img.size, earth_glow, 14))
    d = ImageDraw.Draw(img)
    d.ellipse([ex0, ey0, ex1, ey1], fill=(6, 30, 85, 255), outline=(150, 215, 255, 255), width=2)
    # éclat de lumière sur l'horizon
    img.alpha_composite(_glow_layer(img.size, lambda gd: gd.ellipse(
        [w * 0.74, h * 0.70, w * 0.92, h * 0.90], fill=(170, 220, 255, 200)), 18))

    # Spectre radio le long du bas
    def spectrum(gd):
        base = h - 4
        x = w * 0.42
        while x < w * 0.82:
            env = math.exp(-((x - w * 0.55) / (w * 0.09)) ** 2) * 0.8 + 0.12
            amp = env * rng.uniform(0.15, 1.0) * h * 0.34
            if rng.random() < 0.04:
                amp *= 1.6
            gd.line([(x, base), (x, base - amp)], fill=(60, 160, 255, 210), width=2)
            x += 3

    img.alpha_composite(_glow_layer(img.size, spectrum, 3))
    img.alpha_composite(_glow_layer(img.size, spectrum, 0))

    # Satellite
    sx, sy = w * 0.70, h * 0.30
    img.alpha_composite(_glow_layer(img.size, lambda gd: gd.ellipse(
        [sx - 30, sy - 22, sx + 30, sy + 22], fill=(120, 190, 255, 120)), 16))
    _satellite(img, sx, sy, max(9, h * 0.085))

    # Cadre de l'horloge
    x0, y0, x1, y1 = clock_box
    img.alpha_composite(_glow_layer(img.size, lambda gd: gd.rounded_rectangle(
        [x0, y0, x1, y1], radius=14, outline=(60, 150, 255, 255), width=6), 8))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([x0, y0, x1, y1], radius=14, fill=(3, 10, 30, 235), outline=(90, 170, 255, 255), width=2)

    # Filet lumineux sous le bandeau
    img.alpha_composite(_glow_layer(img.size, lambda gd: gd.line(
        [(0, h - 2), (w, h - 2)], fill=(70, 160, 255, 255), width=3), 3))
    return img.convert("RGB")


def logo_with_glow(logo_rgba, size):
    """Logo rond entouré d'un halo bleu (fond transparent)."""
    pad = 14
    full = size + 2 * pad
    img = Image.new("RGBA", (full, full), (0, 0, 0, 0))
    img.alpha_composite(_glow_layer(img.size, lambda gd: gd.ellipse(
        [pad - 4, pad - 4, full - pad + 4, full - pad + 4], fill=(90, 170, 255, 220)), 9))
    img.alpha_composite(logo_rgba.resize((size, size), Image.LANCZOS), (pad, pad))
    return img


def footer_image(w, h):
    """Pied de page : montagnes, lumières de la ville, pylône d'antenne."""
    w, h = max(200, int(w)), max(30, int(h))
    rng = random.Random(7)
    img = _vgradient(w, h, "#041232", "#020713").convert("RGBA")
    d = ImageDraw.Draw(img)
    _stars(d, w, h * 0.5, rng, int(w / 18))

    # lueur de l'horizon
    img.alpha_composite(_glow_layer(img.size, lambda gd: gd.line(
        [(0, h * 0.62), (w, h * 0.62)], fill=(60, 140, 255, 200), width=6), 10))

    def ridge(y_base, amp, step, color, seed):
        r = random.Random(seed)
        pts = [(0, h)]
        x, y = 0, y_base
        while x <= w + step:
            y = max(h * 0.08, min(h * 0.95, y + r.uniform(-amp, amp)))
            pts.append((x, y))
            x += step
        pts.append((w, h))
        return pts

    far = ridge(h * 0.45, h * 0.12, 18, None, 11)
    d.polygon(far, fill=(18, 44, 100, 255))
    for (xa, ya), (xb, yb) in zip(far[1:-2], far[2:-1]):  # neige sur les crêtes
        if ya < h * 0.38 or yb < h * 0.38:
            d.line([(xa, ya), (xb, yb)], fill=(190, 215, 255, 255), width=1)
    near = ridge(h * 0.72, h * 0.08, 24, None, 23)
    d.polygon(near, fill=(6, 18, 44, 255))

    # Lumières de la ville à droite
    def lights(gd):
        for _ in range(int(w / 6)):
            x = rng.uniform(w * 0.62, w)
            y = rng.uniform(h * 0.70, h - 3)
            r = rng.choice((0.8, 1.1, 1.5))
            gd.ellipse([x - r, y - r, x + r, y + r], fill=(255, rng.randint(170, 220), 90, 255))

    img.alpha_composite(_glow_layer(img.size, lights, 2))
    img.alpha_composite(_glow_layer(img.size, lights, 0))

    # Pylône d'antenne avec ondes, à gauche
    px, top, bot = w * 0.06, h * 0.12, h - 2
    d = ImageDraw.Draw(img)
    col = (25, 50, 95, 255)
    hw = h * 0.16
    d.line([(px - hw, bot), (px, top)], fill=col, width=2)
    d.line([(px + hw, bot), (px, top)], fill=col, width=2)
    for k in range(1, 6):
        y = top + (bot - top) * k / 6
        half = hw * k / 6
        d.line([(px - half, y), (px + half, y)], fill=col, width=1)
    for k in range(1, 4):
        rr = h * 0.08 * k
        box = [px - rr, top - rr, px + rr, top + rr]
        img.alpha_composite(_glow_layer(img.size, lambda gd, b=box: (
            gd.arc(b, 200, 250, fill=(80, 180, 255, 255), width=2),
            gd.arc(b, 290, 340, fill=(80, 180, 255, 255), width=2)), 1))
    return img.convert("RGB")


def gauge_color(frac):
    """Couleur dégradée vert → jaune → orange → rouge selon la position."""
    stops = ((0.0, "#2fd04f"), (0.55, "#9be22d"), (0.72, "#ffd21f"), (0.86, "#ff8a1f"), (1.0, "#f5333a"))
    for (f0, c0), (f1, c1) in zip(stops, stops[1:]):
        if frac <= f1:
            return theme._mix(c0, c1, (frac - f0) / (f1 - f0))
    return stops[-1][1]
