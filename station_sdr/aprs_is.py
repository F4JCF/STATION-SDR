"""Client APRS-IS pour la messagerie APRS (texte) depuis l'appli : réception
des messages adressés à l'indicatif, envoi avec accusés de réception et
réémissions. Passe par Internet (aucune émission radio)."""

import json
import re
import socket
import threading
import time
import unicodedata

SERVER = ("rotate.aprs2.net", 14580)
TOCALL = "APZSDR"
MSG_RE = re.compile(r"^([^>]+)>([^,:]+)[^:]*::([^:]{9}):(.*)$")
RETRY = (30, 60, 120, 240)         # secondes entre les réémissions


def passcode(call: str) -> int:
    base = call.upper().split("-")[0]
    h = 0x73E2
    for i in range(0, len(base), 2):
        h ^= ord(base[i]) << 8
        if i + 1 < len(base):
            h ^= ord(base[i + 1])
    return h & 0x7FFF


def clean_text(text: str) -> str:
    """Texte APRS : ASCII imprimable, sans | ~ {, 67 caractères au plus."""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if 32 <= ord(c) < 127 and c not in "|~{")
    return t[:67]


def valid_call(call: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9]{3,7}(-([0-9]|1[0-5]|[A-Z0-9]{1,2}))?", call.upper()))


class AprsMessenger:
    """Connexion permanente (avec reconnexion) ; les rappels `on_event` sont
    appelés depuis le fil de réception : (kind, data) avec kind dans
    "status", "message", "ack"."""

    def __init__(self, call: str, store_path: str, on_event=None):
        self.call = call.upper()
        self.store_path = store_path
        self.on_event = on_event or (lambda *_a: None)
        self.status = "arrêté"
        self.connected = False
        self._sock = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._seen = {}
        self.history = []
        self.next_id = 1
        self._load()

    # ------------------------------------------------------------ Stockage
    def _load(self):
        try:
            with open(self.store_path, encoding="utf-8") as f:
                data = json.load(f)
            self.history = data.get("history") or []
            self.next_id = int(data.get("next_id") or 1)
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            with self._lock:
                data = {"history": self.history[-2000:], "next_id": self.next_id}
            tmp = self.store_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            import os
            os.replace(tmp, self.store_path)
        except OSError:
            pass

    # ----------------------------------------------------------- Connexion
    def start(self):
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._retry_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        self._set_status("arrêté", False)

    def _set_status(self, text, connected):
        self.status = text
        self.connected = connected
        self.on_event("status", {"text": text, "connected": connected})

    def _run(self):
        delay = 5
        while not self._stop.is_set():
            try:
                self._set_status("connexion…", False)
                sk = socket.create_connection(SERVER, timeout=20)
                sk.settimeout(90)
                self._sock = sk
                f = sk.makefile("r", encoding="latin-1", newline="\n")
                f.readline()
                login = (f"user {self.call} pass {passcode(self.call)} vers STATION-SDR 1.31 "
                         f"filter g/{self.call.split('-')[0]}*\r\n")
                sk.sendall(login.encode())
                verified = None
                for _ in range(5):
                    line = f.readline()
                    if "logresp" in line:
                        verified = " verified" in line and "unverified" not in line
                        break
                if not verified:
                    self._set_status("refusé (indicatif non vérifié)", False)
                    sk.close()
                    self._stop.wait(300)
                    continue
                server = SERVER[0]
                self._set_status(f"connecté ({server})", True)
                delay = 5
                while not self._stop.is_set():
                    line = f.readline()
                    if not line:
                        raise OSError("connexion fermée")
                    line = line.rstrip("\r\n")
                    if line and not line.startswith("#"):
                        self._handle(line)
            except (OSError, socket.timeout) as exc:
                if self._stop.is_set():
                    break
                self._set_status(f"déconnecté ({exc}) — nouvel essai dans {delay} s", False)
                self._stop.wait(delay)
                delay = min(300, delay * 2)
            finally:
                try:
                    if self._sock:
                        self._sock.close()
                except OSError:
                    pass
                self._sock = None

    def _send_line(self, line: str) -> bool:
        sk = self._sock
        if not sk or not self.connected:
            return False
        try:
            sk.sendall((line + "\r\n").encode("ascii", "replace"))
            return True
        except OSError:
            return False

    # ------------------------------------------------------------ Réception
    def _handle(self, line):
        m = MSG_RE.match(line)
        if not m:
            return
        src, addressee, body = m.group(1), m.group(3).strip().upper(), m.group(4)
        if addressee.split("-")[0] != self.call.split("-")[0] or addressee.startswith("BLN"):
            return
        # Accusés reçus pour nos messages
        am = re.match(r"^(ack|rej)([A-Za-z0-9]{1,5})\s*$", body)
        if am:
            self._got_ack(src, am.group(2), am.group(1) == "ack")
            return
        text, mid = body, None
        if "{" in body:
            text, rest = body.rsplit("{", 1)
            mid = rest.split("}", 1)[0][:5].strip() or None
            # Réponse-accusé (« reply-ack ») : {MM}AA
            if "}" in rest and rest.split("}", 1)[1].strip():
                self._got_ack(src, rest.split("}", 1)[1].strip(), True)
        key = (src, mid or text)
        now = time.time()
        if mid and addressee == self.call:
            self._send_line(f"{self.call}>{TOCALL},TCPIP*::{src:<9}:ack{mid}")
        if now - self._seen.get(key, 0) < 1800:
            return                       # doublon (réémission)
        self._seen[key] = now
        entry = {"t": now, "dir": "in", "peer": src, "to": addressee, "text": text.strip(), "id": mid,
                 "unread": True, "acked": addressee == self.call and bool(mid)}
        with self._lock:
            self.history.append(entry)
        self.save()
        self.on_event("message", entry)

    def _got_ack(self, src, mid, ok):
        changed = None
        with self._lock:
            for e in reversed(self.history):
                if e["dir"] == "out" and e["peer"] == src and e.get("id") == mid and e.get("state") == "envoi":
                    e["state"] = "reçu" if ok else "refusé"
                    changed = e
                    break
        if changed:
            self.save()
            self.on_event("ack", changed)

    # --------------------------------------------------------------- Envoi
    def send(self, peer: str, text: str) -> dict:
        peer = peer.upper().strip()
        text = clean_text(text)
        if not valid_call(peer):
            raise ValueError("indicatif destinataire invalide")
        if not text:
            raise ValueError("message vide")
        with self._lock:
            mid = str(self.next_id)
            self.next_id = self.next_id % 99999 + 1
            entry = {"t": time.time(), "dir": "out", "peer": peer, "text": text, "id": mid, "state": "envoi",
                     "tries": 0, "next": 0}
            self.history.append(entry)
        self._transmit(entry)
        self.save()
        return entry

    def _transmit(self, e):
        ok = self._send_line(f"{self.call}>{TOCALL},TCPIP*::{e['peer']:<9}:{e['text']}{{{e['id']}")
        if ok:
            e["tries"] = e.get("tries", 0) + 1
        e["next"] = time.time() + RETRY[min(len(RETRY) - 1, max(0, e.get("tries", 1) - 1))]

    def _retry_loop(self):
        while not self._stop.wait(5):
            changed = False
            with self._lock:
                pending = [e for e in self.history[-200:] if e["dir"] == "out" and e.get("state") == "envoi"]
            for e in pending:
                if time.time() < e.get("next", 0):
                    continue
                if e.get("tries", 0) > len(RETRY):
                    e["state"] = "sans accusé"
                    self.on_event("ack", e)
                    changed = True
                    continue
                self._transmit(e)
                changed = True
            if changed:
                self.save()

    # ------------------------------------------------------------- Vues
    def conversations(self):
        with self._lock:
            conv = {}
            for e in self.history:
                c = conv.setdefault(e["peer"], {"peer": e["peer"], "last": 0, "unread": 0, "count": 0})
                c["last"] = max(c["last"], e["t"])
                c["count"] += 1
                if e.get("unread"):
                    c["unread"] += 1
        return sorted(conv.values(), key=lambda c: -c["last"])

    def thread(self, peer):
        with self._lock:
            return [dict(e) for e in self.history if e["peer"] == peer]

    def mark_read(self, peer):
        with self._lock:
            n = 0
            for e in self.history:
                if e["peer"] == peer and e.get("unread"):
                    e["unread"] = False
                    n += 1
        if n:
            self.save()

    def unread(self):
        with self._lock:
            return sum(1 for e in self.history if e.get("unread"))
