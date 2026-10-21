"""Informations complémentaires sur les avions, comme dans tar1090.

* Base tar1090 hébergée par le Pi (port de tar1090) : immatriculation,
  type, désignation (ex. L2J), catégorie de turbulence, drapeaux
  (militaire, intéressant, PIA, LADD), compagnie d'après l'indicatif.
* Routes (aéroports de départ / d'arrivée) : service public adsb.im,
  celui qu'utilise tar1090 par défaut.

Tout est mis en cache et interrogé en arrière-plan : l'interface n'attend
jamais le réseau, elle affiche ce qui est déjà connu.
"""

import gzip
import json
import re
import threading
import time

import requests

TIMEOUT = 8
ROUTE_URL = "http://adsb.im/api/0/routeset"  # adsb.im demande http plutôt que https


def _decode(resp) -> object:
    """Les fichiers de la base tar1090 sont servis gzippés ; selon le
    serveur web, l'en-tête Content-Encoding est présent ou non."""
    data = resp.content
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data.decode("utf-8", "replace"))


class TarDb:
    def __init__(self, base_url_fn):
        self._base_url_fn = base_url_fn  # renvoie http://pi:port
        self._folder = None
        self._folder_checked = 0.0
        self._shards = {}          # "4C" -> dict
        self._records = {}         # hex -> dict ou None
        self._types = None
        self._operators = None
        self._lock = threading.Lock()
        self._pending = set()
        self.available = None      # None = inconnu, False = pas de base sur le Pi

    def _base(self):
        return self._base_url_fn().rstrip("/")

    def _get_folder(self):
        if self._folder or time.time() - self._folder_checked < 300:
            return self._folder
        self._folder_checked = time.time()
        try:
            html = requests.get(self._base() + "/", timeout=TIMEOUT).text
            m = re.search(r'databaseFolder\s*=\s*"([^"]+)"', html)
            self._folder = m.group(1) if m else "db2"
        except requests.RequestException:
            self._folder = None
        return self._folder

    def _get_json(self, name):
        folder = self._get_folder()
        if not folder:
            return None
        resp = requests.get(f"{self._base()}/{folder}/{name}", timeout=TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return _decode(resp)

    def _load_tables(self):
        if self._types is None:
            try:
                self._types = self._get_json("icao_aircraft_types2.js") or {}
            except Exception:
                self._types = {}
        if self._operators is None:
            try:
                self._operators = self._get_json("operators.js") or {}
            except Exception:
                self._operators = {}

    def _lookup_hex(self, hx):
        icao = hx.upper()
        level = 1
        while level <= 6:
            bkey, dkey = icao[:level], icao[level:]
            shard = self._shards.get(bkey)
            if shard is None:
                shard = self._get_json(f"{bkey}.js")
                if shard is None:
                    self.available = False if not self._shards else self.available
                    return None
                self._shards[bkey] = shard
                self.available = True
            if dkey in shard:
                return shard[dkey]
            children = shard.get("children") or []
            if bkey + dkey[:1] in children:
                level += 1
                continue
            return None
        return None

    def request(self, hexes):
        """Lance en arrière-plan la recherche des avions pas encore connus."""
        todo = [h for h in hexes if h and not h.startswith("~") and h not in self._records and h not in self._pending]
        if not todo:
            return
        with self._lock:
            self._pending.update(todo)

        def worker():
            try:
                self._load_tables()
                for hx in todo:
                    try:
                        rec = self._lookup_hex(hx)
                    except Exception:
                        rec = None
                    self._records[hx] = self._format(rec)
            finally:
                with self._lock:
                    self._pending.difference_update(todo)

        threading.Thread(target=worker, daemon=True).start()

    def _format(self, rec):
        if not rec:
            return {}
        reg, typ = (rec[0] if len(rec) > 0 else None), (rec[1] if len(rec) > 1 else None)
        flags = rec[2] if len(rec) > 2 and isinstance(rec[2], str) else ""
        out = {"r": reg or None, "t": typ or None, "long": rec[3] if len(rec) > 3 and rec[3] else None}
        names = ("militaire", "intéressant", "PIA", "LADD")
        out["flags"] = [n for i, n in enumerate(names) if len(flags) > i and flags[i] == "1"]
        if typ and self._types:
            td = self._types.get(str(typ).upper())
            if td:
                out["long"] = out["long"] or td[0]
                out["desc"] = td[1] if len(td) > 1 else None
                out["wtc"] = td[2] if len(td) > 2 else None
        return out

    def get(self, hx):
        return self._records.get(hx) or {}

    def airline(self, callsign, registration=None):
        """Compagnie d'après les 3 premières lettres de l'indicatif (même
        règle que tar1090)."""
        if not self._operators or not callsign:
            return None
        cs = re.sub(r"\s", "", callsign).upper()
        if len(cs) < 4 or re.fullmatch(r"[A-Z]{4}", cs[:4]) or not re.fullmatch(r"[A-Z]{3}", cs[:3]):
            return None
        if registration and re.sub(r"[-+]", "", registration).upper() == cs:
            return None
        op = self._operators.get(cs[:3])
        return op if isinstance(op, dict) else None


class RouteCache:
    """Routes par indicatif via adsb.im (cache 6 h, une requête groupée
    au plus toutes les 15 s)."""

    def __init__(self):
        self._cache = {}       # indicatif -> (expire, route dict ou None)
        self._last = 0.0
        self._busy = False

    def get(self, callsign):
        hit = self._cache.get((callsign or "").strip().upper())
        return hit[1] if hit else None

    def request(self, planes):
        """planes : liste de (indicatif, lat, lon)."""
        now = time.time()
        if self._busy or now - self._last < 15:
            return
        todo = []
        for cs, lat, lon in planes:
            cs = (cs or "").strip().upper()
            if not cs or lat is None or lon is None:
                continue
            hit = self._cache.get(cs)
            if hit and hit[0] > now:
                continue
            todo.append({"callsign": cs, "lat": lat, "lng": lon})
        if not todo:
            return
        todo = todo[:40]
        self._busy, self._last = True, now

        def worker():
            try:
                resp = requests.post(ROUTE_URL, json={"planes": todo}, timeout=TIMEOUT)
                resp.raise_for_status()
                routes = resp.json() or []
                got = set()
                for r in routes:
                    if isinstance(r, dict) and r.get("callsign"):
                        cs = r["callsign"].strip().upper()
                        airports = r.get("_airports") or []
                        ok = airports and r.get("plausible", True) not in (False, 0)
                        self._cache[cs] = (time.time() + 6 * 3600, {
                            "airports": [{"icao": a.get("icao"), "iata": a.get("iata"),
                                          "city": a.get("location"), "name": a.get("name")} for a in airports],
                            "plausible": bool(r.get("plausible", True)),
                        } if ok else None)
                        got.add(cs)
                for p in todo:
                    if p["callsign"] not in got:
                        self._cache[p["callsign"]] = (time.time() + 1800, None)
            except Exception:
                # service injoignable : on réessaiera plus tard
                for p in todo:
                    self._cache[p["callsign"]] = (time.time() + 600, None)
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True).start()
