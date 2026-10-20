#!/usr/bin/env python3
"""
API JSON des paquets APRS reçus par Direwolf (F4JCF-SDR-STATION)

Se connecte au port KISS TCP de Direwolf (local au Raspberry), décode les
trames AX.25/APRS (même logique éprouvée que aprs_nextion.py), et expose
le résultat en JSON pour que des applications tierces (comme STATION-SDR)
puissent afficher les stations entendues sans avoir à parler KISS/AX.25
elles-mêmes.

Endpoints :
  GET /api/aprs/stations  -> dernière position connue de chaque station
                              entendue (une entrée par callsign)
  GET /api/aprs/packets   -> historique des derniers paquets décodés
                              (toutes stations confondues, les plus
                              récents en dernier)
  GET /api/aprs/health    -> état de la connexion à Direwolf

Configuration (variables d'environnement, toutes optionnelles) :
  DIREWOLF_HOST       (défaut 127.0.0.1)
  DIREWOLF_KISS_PORT  (défaut 8001 - vérifier la ligne "KISSPORT" dans
                       direwolf.conf si ça ne correspond pas)
  APRS_API_PORT       (défaut 5004)
"""

import logging
import math
import os
import re
import socket
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify

DIREWOLF_HOST = os.environ.get("DIREWOLF_HOST", "127.0.0.1")
DIREWOLF_PORT = int(os.environ.get("DIREWOLF_KISS_PORT", "8001"))
API_PORT = int(os.environ.get("APRS_API_PORT", "5004"))

# Position de la station (Mougins) - utilisée pour calculer distance/direction.
HOME_LAT = 43.59143179444281
HOME_LON = 7.005057292489045

MAX_PACKETS = 300

LOG_FILE = os.environ.get("APRS_API_LOG", "/home/f4jcf/aprs_api.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("aprs_api")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# ÉTAT PARTAGÉ (protégé par un verrou, lu par Flask, écrit par le thread KISS)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_stations = {}       # callsign -> dernière entrée connue
_recent_packets = []  # historique, le plus récent en dernier
_direwolf_connected = False
_last_frame_at = None

# ---------------------------------------------------------------------------
# GÉOGRAPHIE
# ---------------------------------------------------------------------------


def haversine_distance_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def bearing_degrees(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass_point(deg):
    points = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return points[int((deg + 11.25) // 22.5) % 16]

# ---------------------------------------------------------------------------
# DÉSENCAPSULATION KISS (reprise telle quelle de aprs_nextion.py, éprouvée
# sur ce même type de flux KISS TCP)
# ---------------------------------------------------------------------------

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD


def extract_kiss_frames(buffer: bytearray):
    """Extrait les trames KISS complètes présentes dans buffer.
    Retourne (liste_de_trames_ax25, reste_du_buffer)."""
    frames = []
    while True:
        try:
            start = buffer.index(FEND)
        except ValueError:
            buffer.clear()
            break
        try:
            end = buffer.index(FEND, start + 1)
        except ValueError:
            del buffer[:start]
            break
        raw = bytes(buffer[start + 1:end])
        del buffer[:end + 1]
        if len(raw) < 2:
            continue
        port_cmd = raw[0]
        if (port_cmd & 0x0F) != 0x00:
            continue  # pas une trame de données
        payload = raw[1:]
        unescaped = bytearray()
        i = 0
        while i < len(payload):
            b = payload[i]
            if b == FESC and i + 1 < len(payload):
                nxt = payload[i + 1]
                if nxt == TFEND:
                    unescaped.append(FEND)
                    i += 2
                    continue
                elif nxt == TFESC:
                    unescaped.append(FESC)
                    i += 2
                    continue
            unescaped.append(b)
            i += 1
        frames.append(bytes(unescaped))
    return frames, buffer

# ---------------------------------------------------------------------------
# DÉCODAGE AX.25 + POSITION APRS (reprise de aprs_nextion.py)
# ---------------------------------------------------------------------------


def decode_ax25_addr(raw, offset):
    call_bytes = raw[offset:offset + 6]
    call = "".join(chr(b >> 1) for b in call_bytes).strip()
    ssid_byte = raw[offset + 6]
    ssid = (ssid_byte >> 1) & 0x0F
    last = bool(ssid_byte & 0x01)
    if ssid:
        call = f"{call}-{ssid}"
    return call, last


def decode_ax25_frame(raw):
    """Retourne (source_callsign, dest, chemin_digipeaters, payload_text)
    ou None si trame invalide."""
    if len(raw) < 16:
        return None
    try:
        dest, _ = decode_ax25_addr(raw, 0)
        src, last = decode_ax25_addr(raw, 7)
        offset = 14
        path = []
        while not last:
            if offset + 7 > len(raw):
                return None
            addr, last = decode_ax25_addr(raw, offset)
            path.append(addr)
            offset += 7
        offset += 2  # control + PID
        payload = raw[offset:]
        text = payload.decode("latin-1", errors="replace")
        return src, dest, path, text
    except Exception as e:
        log.warning(f"Erreur decode AX25: {e}")
        return None


APRS_POS_RE = re.compile(
    r"[!=/@](\d{2})(\d{2}\.\d{2})([NS])(\S)(\d{3})(\d{2}\.\d{2})([EW])(\S)(.*)"
)


def parse_aprs_position(text):
    """Extrait lat/lon (+ symbole/commentaire) d'un payload APRS en
    position non-compressée. Retourne None si ce n'est pas un paquet de
    position reconnu (Mic-E et format compressé ne sont pas décodés ici)."""
    m = APRS_POS_RE.search(text)
    if not m:
        return None
    lat_deg, lat_min, lat_hem, table, lon_deg, lon_min, lon_hem, code, comment = m.groups()
    lat = int(lat_deg) + float(lat_min) / 60.0
    lon = int(lon_deg) + float(lon_min) / 60.0
    if lat_hem == "S":
        lat = -lat
    if lon_hem == "W":
        lon = -lon
    return {
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "symbol": f"{table}{code}",
        "comment": comment.strip(),
    }

# ---------------------------------------------------------------------------
# STOCKAGE DES PAQUETS DÉCODÉS
# ---------------------------------------------------------------------------


def _store_packet(src, dest, path, text, position):
    now = time.time()
    entry = {
        "callsign": src,
        "dest": dest,
        "path": path,
        "raw": text,
        "last_heard": now,
        "last_heard_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
    }
    if position:
        dist = haversine_distance_km(HOME_LAT, HOME_LON, position["lat"], position["lon"])
        brg = bearing_degrees(HOME_LAT, HOME_LON, position["lat"], position["lon"])
        entry.update({
            "lat": position["lat"],
            "lon": position["lon"],
            "symbol": position["symbol"],
            "comment": position["comment"],
            "distance_km": round(dist, 1),
            "bearing_deg": round(brg),
            "bearing_compass": compass_point(brg),
        })

    with _lock:
        _stations[src] = entry
        _recent_packets.append(entry)
        if len(_recent_packets) > MAX_PACKETS:
            del _recent_packets[: len(_recent_packets) - MAX_PACKETS]


# ---------------------------------------------------------------------------
# LECTURE DU FLUX KISS DE DIREWOLF (boucle de fond, reconnexion automatique)
# ---------------------------------------------------------------------------


def kiss_reader_loop():
    global _direwolf_connected, _last_frame_at
    while True:
        try:
            sock = socket.create_connection((DIREWOLF_HOST, DIREWOLF_PORT), timeout=10)
            sock.settimeout(1.0)
            with _lock:
                _direwolf_connected = True
            log.info(f"Connecté au flux KISS de Direwolf {DIREWOLF_HOST}:{DIREWOLF_PORT}")
            buffer = bytearray()

            while True:
                try:
                    data = sock.recv(1024)
                    if not data:
                        raise ConnectionError("Direwolf a fermé la connexion KISS")
                    buffer.extend(data)
                except socket.timeout:
                    continue

                frames, buffer = extract_kiss_frames(buffer)
                for raw in frames:
                    decoded = decode_ax25_frame(raw)
                    if not decoded:
                        continue
                    src, dest, path, text = decoded
                    position = parse_aprs_position(text)
                    _store_packet(src, dest, path, text, position)
                    with _lock:
                        _last_frame_at = time.time()

        except Exception as e:
            with _lock:
                _direwolf_connected = False
            log.error(f"Erreur connexion Direwolf ({DIREWOLF_HOST}:{DIREWOLF_PORT}): {e}. Nouvel essai dans 5s")
            time.sleep(5)

# ---------------------------------------------------------------------------
# API FLASK
# ---------------------------------------------------------------------------


@app.get("/api/aprs/stations")
def api_stations():
    with _lock:
        return jsonify(sorted(_stations.values(), key=lambda s: s["last_heard"], reverse=True))


@app.get("/api/aprs/packets")
def api_packets():
    with _lock:
        return jsonify(list(_recent_packets))


@app.get("/api/aprs/health")
def api_health():
    with _lock:
        return jsonify({
            "direwolf_host": DIREWOLF_HOST,
            "direwolf_port": DIREWOLF_PORT,
            "direwolf_connected": _direwolf_connected,
            "stations_count": len(_stations),
            "last_frame_at": _last_frame_at,
        })


if __name__ == "__main__":
    threading.Thread(target=kiss_reader_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=API_PORT)
