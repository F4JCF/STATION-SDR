"""Impacts de foudre en temps réel (réseau communautaire Blitzortung.org,
usage privé non commercial). Connexion WebSocket en arrière-plan ; on ne
garde que les impacts proches de la station (600 km) de la dernière heure."""

import json
import math
import random
import threading
import time

HOSTS = ("ws1.blitzortung.org", "ws7.blitzortung.org", "ws8.blitzortung.org", "ws3.blitzortung.org")
KEEP_KM = 600
KEEP_S = 3600


def decode(b: str) -> str:
    """Les messages sont compressés (LZW sur caractères), comme sur la carte web."""
    if not b:
        return b
    d = {}
    chars = list(b)
    c = f = chars[0]
    out = [c]
    code = 256
    for ch in chars[1:]:
        n = ord(ch)
        if n < 256:
            a = ch
        else:
            a = d.get(n, f + c)
        out.append(a)
        c = a[0]
        d[code] = f + c
        code += 1
        f = a
    return "".join(out)


def dist_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(min(1.0, h)))


class Lightning:
    def __init__(self, lat, lon, on_strike=None):
        self.lat, self.lon = lat, lon
        self.on_strike = on_strike or (lambda s: None)
        self.strikes = []          # (temps, lat, lon, km)
        self.status = "arrêté"
        self.connected = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ws = None

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def recent(self, seconds=1800, km=KEEP_KM):
        now = time.time()
        with self._lock:
            return [s for s in self.strikes if now - s[0] <= seconds and s[3] <= km]

    def _add(self, lat, lon, t):
        km = dist_km(self.lat, self.lon, lat, lon)
        if km > KEEP_KM:
            return
        s = (t, lat, lon, km)
        with self._lock:
            self.strikes.append(s)
            cut = time.time() - KEEP_S
            if len(self.strikes) > 5000 or (self.strikes and self.strikes[0][0] < cut):
                self.strikes = [x for x in self.strikes if x[0] >= cut][-5000:]
        self.on_strike(s)

    def _run(self):
        try:
            import websocket
        except ImportError:
            self.status = "module websocket absent"
            return
        delay = 5
        while not self._stop.is_set():
            host = random.choice(HOSTS)
            try:
                self.status = f"connexion à {host}…"
                ws = websocket.create_connection(f"wss://{host}/", timeout=30,
                                                 header=["Origin: https://map.blitzortung.org"])
                self._ws = ws
                ws.send(json.dumps({"a": 111}))
                self.connected = True
                self.status = "connecté (Blitzortung.org)"
                delay = 5
                while not self._stop.is_set():
                    msg = ws.recv()
                    if not msg:
                        raise ConnectionError("connexion fermée")
                    if isinstance(msg, bytes):
                        msg = msg.decode("utf-8", "replace")
                    try:
                        data = json.loads(decode(msg))
                    except ValueError:
                        try:
                            data = json.loads(msg)
                        except ValueError:
                            continue
                    lat, lon = data.get("lat"), data.get("lon")
                    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                        t = data.get("time")
                        t = t / 1e9 if isinstance(t, (int, float)) and t > 1e12 else time.time()
                        self._add(float(lat), float(lon), t)
            except Exception as exc:
                self.connected = False
                if self._stop.is_set():
                    break
                self.status = f"déconnecté ({str(exc)[:60]}) — nouvel essai dans {delay} s"
                self._stop.wait(delay)
                delay = min(300, delay * 2)
            finally:
                try:
                    if self._ws:
                        self._ws.close()
                except Exception:
                    pass
                self._ws = None
        self.connected = False
