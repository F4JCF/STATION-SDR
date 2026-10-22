"""Décodage minimal de trames APRS au format texte (TNC2) :
« SOURCE>DEST,CHEMIN:données ». Positions en clair, compressées et Mic-E.
"""

import re

_POS_RE = re.compile(r"(\d{2})([\d ]{2}\.[\d ]{2})([NS])(.)(\d{3})([\d ]{2}\.[\d ]{2})([EW])(.)")


def parse_tnc2(line: str):
    """Renvoie {"src", "dest", "path", "info"} ou None."""
    line = (line or "").strip()
    # Le firmware LoRa remplace les octets d'en-tête non imprimables par « . »
    line = re.sub(r"^[.<\xff\x01]+", "", line)
    if ">" not in line or ":" not in line:
        return None
    head, _, info = line.partition(":")
    src, _, rest = head.partition(">")
    parts = rest.split(",")
    if not src or not parts[0]:
        return None
    return {"src": src.strip(), "dest": parts[0].strip(), "path": [p for p in parts[1:] if p], "info": info}


def _uncompressed(body):
    m = _POS_RE.match(body)
    if not m:
        return None
    try:
        lat = int(m.group(1)) + float(m.group(2).replace(" ", "0")) / 60
        lon = int(m.group(5)) + float(m.group(6).replace(" ", "0")) / 60
    except ValueError:
        return None
    if m.group(3) == "S":
        lat = -lat
    if m.group(7) == "W":
        lon = -lon
    return {"lat": lat, "lon": lon, "symbol": m.group(4) + m.group(8), "comment": body[19:].strip()}


def _b91(s):
    v = 0
    for c in s:
        v = v * 91 + (ord(c) - 33)
    return v


def _compressed(body):
    if len(body) < 13:
        return None
    table, y, x, code = body[0], body[1:5], body[5:9], body[9]
    if not all(33 <= ord(c) <= 124 for c in y + x):
        return None
    lat = 90 - _b91(y) / 380926
    lon = -180 + _b91(x) / 190463
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"lat": lat, "lon": lon, "symbol": table + code, "comment": body[13:].strip()}


def _mic_e(dest, info):
    """Mic-E : la latitude est codée dans le champ destination."""
    d = dest.split("-")[0]
    if len(d) < 6 or len(info) < 9:
        return None
    digits = []
    for c in d[:6]:
        if "0" <= c <= "9":
            digits.append(int(c))
        elif "A" <= c <= "J":
            digits.append(ord(c) - ord("A"))
        elif "P" <= c <= "Y":
            digits.append(ord(c) - ord("P"))
        elif c in "KLZ":
            digits.append(0)
        else:
            return None
    lat = digits[0] * 10 + digits[1] + (digits[2] * 10 + digits[3] + (digits[4] * 10 + digits[5]) / 100) / 60
    north = "P" <= d[3] <= "Z"
    lon_offset = 100 if "P" <= d[4] <= "Z" else 0
    west = "P" <= d[5] <= "Z"
    if not north:
        lat = -lat
    try:
        deg = ord(info[1]) - 28 + lon_offset
        if 180 <= deg <= 189:
            deg -= 80
        elif 190 <= deg <= 199:
            deg -= 190
        mins = ord(info[2]) - 28
        if mins >= 60:
            mins -= 60
        hund = ord(info[3]) - 28
    except IndexError:
        return None
    lon = deg + (mins + hund / 100) / 60
    if west:
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"lat": lat, "lon": lon, "symbol": info[8] + info[7], "comment": info[9:].strip()}


def parse_position(dest: str, info: str):
    """Position d'une trame APRS, ou None (statut, message, télémétrie...)."""
    if not info:
        return None
    t = info[0]
    if t in "!=":
        body = info[1:]
    elif t in "/@":
        body = info[8:]  # horodatage de 7 caractères
    elif t in "`'\x1c\x1d":
        return _mic_e(dest, info)
    elif t == ";" and len(info) > 18:  # objet
        body = info[18:]
    else:
        return None
    if body[:1].isdigit():
        return _uncompressed(body)
    return _compressed(body)
