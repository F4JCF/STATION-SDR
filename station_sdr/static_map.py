"""Carte fixe (image) pour les comptes rendus : tuiles OpenStreetMap ou IGN
assemblées, avec traits, cercles et repères dessinés par-dessus."""

import io
import math

from PIL import Image, ImageDraw, ImageFont

from . import map_tiles

TILE = 256


def _xy(lat, lon, z):
    n = 2 ** z
    x = (lon + 180) / 360 * n * TILE
    lr = math.radians(lat)
    y = (1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * n * TILE
    return x, y


def _font(size):
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(points, lines=(), circles=(), markers=(), size=(1000, 650), style="osm", pad=0.15):
    """points : [(lat, lon)] à faire tenir dans l'image ;
    lines : [((lat, lon), (lat, lon), couleur)] ; circles : [([(lat, lon)…], couleur)] ;
    markers : [(lat, lon, texte, couleur)]. Renvoie des octets PNG (ou None)."""
    if not points:
        return None
    w, h = size
    tpl = map_tiles.STYLES.get(style, map_tiles.STYLES["osm"])["url"]
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    zoom = 15
    for z in range(15, 4, -1):
        x0, y0 = _xy(max(lats), min(lons), z)
        x1, y1 = _xy(min(lats), max(lons), z)
        if (x1 - x0) * (1 + 2 * pad) <= w and (y1 - y0) * (1 + 2 * pad) <= h:
            zoom = z
            break
        zoom = z
    cx, cy = _xy((max(lats) + min(lats)) / 2, (max(lons) + min(lons)) / 2, zoom)
    left, top = cx - w / 2, cy - h / 2
    img = Image.new("RGB", (w, h), (230, 230, 230))
    for tx in range(int(left // TILE), int((left + w) // TILE) + 1):
        for ty in range(int(top // TILE), int((top + h) // TILE) + 1):
            if ty < 0 or ty >= 2 ** zoom:
                continue
            url = tpl.format(z=zoom, x=tx % (2 ** zoom), y=ty)
            try:
                r = map_tiles._get(url, timeout=10)
                if r.status_code == 200:
                    tile = Image.open(io.BytesIO(r.content)).convert("RGB")
                    img.paste(tile, (int(tx * TILE - left), int(ty * TILE - top)))
            except Exception:
                continue
    d = ImageDraw.Draw(img, "RGBA")

    def P(lat, lon):
        x, y = _xy(lat, lon, zoom)
        return x - left, y - top

    for pts, col in circles:
        d.line([P(*p) for p in pts], fill=col, width=4)
    for a, b, col in lines:
        d.line([P(*a), P(*b)], fill=col, width=4)
    f = _font(15)
    for lat, lon, text, col in markers:
        x, y = P(lat, lon)
        d.ellipse([x - 7, y - 7, x + 7, y + 7], fill=col, outline="white", width=2)
        if text:
            tw = d.textlength(text, font=f) if hasattr(d, "textlength") else len(text) * 8
            d.rectangle([x + 9, y - 10, x + 15 + tw, y + 10], fill=(255, 255, 255, 210))
            d.text((x + 12, y - 9), text, fill=col, font=f)
    credit = map_tiles.STYLES.get(style, map_tiles.STYLES["osm"])["credit"]
    fs = _font(12)
    tw = d.textlength(credit, font=fs) if hasattr(d, "textlength") else len(credit) * 7
    d.rectangle([w - tw - 10, h - 20, w, h], fill=(255, 255, 255, 220))
    d.text((w - tw - 5, h - 18), credit, fill=(40, 40, 40), font=fs)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()
