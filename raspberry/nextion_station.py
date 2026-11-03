#!/usr/bin/env python3
"""
Résumé de la station sur l'écran Nextion (NX4832K035, 480×320, liaison série).

L'écran est dessiné directement par commandes (cls, xstr) : aucune
modification du projet Nextion n'est nécessaire, il suffit qu'il contienne
au moins une police (n° 0). Un appui n'importe où sur l'écran fait passer à
l'écran suivant ; sinon les écrans défilent seuls.

  1. Station : clé SDR, avions, navires, APRS, Winlink, météo, santé du Pi
  2. Alertes : les dernières notifications
  3. Satellites : prochains passages (Meteor, ISS)

Réglages : /etc/nextion-station.json
  {"port": "/dev/ttyAMA0", "baud": 9600, "font": 0, "font_big": 0,
   "row_h": 34, "cycle_s": 20, "dim": 80}
"""

import json
import logging
import threading
import time
import urllib.request

import serial

CONF_FILE = "/etc/nextion-station.json"
END = b"\xff\xff\xff"
BLACK, WHITE, GRAY, ORANGE, GREEN, RED, CYAN, YELLOW, DARK = 0, 65535, 33808, 64512, 2016, 63488, 2047, 65504, 4228

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nextion-station")

conf = {"port": "/dev/ttyAMA0", "baud": 9600, "font": 0, "font_big": 0, "row_h": 34, "cycle_s": 20, "dim": 80}
MODES = {"adsb": "ADS-B", "ais": "AIS", "aprs": "APRS", "openwebrx": "OpenWebRX", "beacon": "Balise 406",
         "elt121": "Veille 121,5", "pocsag": "POCSAG", "radiosonde": "Radiosondes", "meteor": "Satellites",
         "433": "Capteurs 433", "iss": "APRS ISS", "ecoute": "Ecoute", "off": "Arretee"}


def get(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


class Screen:
    def __init__(self):
        self.ser = serial.Serial(conf["port"], int(conf["baud"]), timeout=0.1)
        self.lock = threading.Lock()
        self.page = 0
        self.touched = threading.Event()

    def cmd(self, text):
        with self.lock:
            self.ser.write(text.encode("latin-1", "replace") + END)

    def text(self, x, y, w, h, txt, color=WHITE, bg=BLACK, big=False, center=False):
        txt = str(txt).replace('"', "'")
        font = conf["font_big"] if big else conf["font"]
        self.cmd(f'xstr {x},{y},{w},{h},{font},{color},{bg},{1 if center else 0},1,1,"{txt}"')

    def setup(self):
        for c in ("bkcmd=0", "page 0", "sendxy=1", "thsp=0", f"dim={int(conf['dim'])}", "sleep=0"):
            self.cmd(c)

    def listen_touch(self):
        """Événements de l'écran : 0x67/0x68 (coordonnées) ou 0x65 (composant)."""
        buf = b""
        while True:
            try:
                data = self.ser.read(64)
            except serial.SerialException:
                time.sleep(1)
                continue
            if not data:
                continue
            buf += data
            while END in buf:
                frame, buf = buf.split(END, 1)
                if frame and frame[0] in (0x65, 0x67):
                    self.touched.set()

    # ------------------------------------------------------------ Écrans
    def header(self, title):
        self.cmd(f"cls {BLACK}")
        self.cmd(f"fill 0,0,480,40,{DARK}")
        self.text(8, 2, 330, 36, title, ORANGE, DARK, big=True)
        self.text(340, 2, 132, 36, time.strftime("%H:%M"), WHITE, DARK, big=True, center=True)

    def row(self, i, label, value, color=WHITE):
        y = 46 + i * int(conf["row_h"])
        h = int(conf["row_h"]) - 2
        self.text(8, y, 200, h, label, GRAY)
        self.text(210, y, 262, h, value, color)

    def draw_station(self):
        d = get("http://127.0.0.1:5008/api/dash") or {}
        m = (d.get("mode") or {}).get("mode")
        a = d.get("activity") or {}
        h = d.get("health") or {}
        th = h.get("throttled") or {}
        self.header("F4JCF-SDR-STATION")
        self.row(0, "Cle SDR", MODES.get(m, m or "?"), ORANGE if m and m != "off" else RED)
        self.row(1, "Avions", a.get("aircraft", "en pause") if m == "adsb" else "en pause",
                 WHITE if m == "adsb" else GRAY)
        self.row(2, "Navires", a.get("ships", "en pause") if m == "ais" else "en pause",
                 WHITE if m == "ais" else GRAY)
        self.row(3, "Stations APRS", f"{a.get('aprs', '-')} (30 min)")
        unread = a.get("winlink_unread")
        self.row(4, "Winlink", f"{unread} non lu(s)" if unread else "rien de nouveau", ORANGE if unread else WHITE)
        w = get("http://127.0.0.1:5001/api/meteo") or {}
        t = w.get("temperature_c")
        wx = get("http://127.0.0.1:5008/api/wx/status") or {}
        garden = ((wx.get("selected") or {}).get("values") or {}).get("temp_c")
        meteo = (f"{t} C" if t is not None else "-") + (f" / jardin {garden:.1f} C" if garden is not None else "")
        self.row(5, "Meteo", meteo)
        temp = h.get("cpu_temp_c")
        power = "SOUS-TENSION" if th.get("under_voltage_now") else "alim. OK"
        self.row(6, "Pi", f"{temp:.0f} C - {power}" if temp is not None else power,
                 RED if th.get("under_voltage_now") or (temp or 0) >= 75 else GREEN)
        if a.get("elt121"):
            e = a["elt121"]
            self.row(7, "Veille 121,5", "SIRENE !" if e.get("active") else f"ecoute (+{e.get('above_db', 0)} dB)",
                     RED if e.get("active") else GREEN)
        else:
            alerts = d.get("alerts") or []
            self.row(7, "Derniere alerte", alerts[0]["title"][:28] if alerts else "aucune",
                     RED if alerts and time.time() - alerts[0]["time"] < 3600 else GRAY)

    def draw_alerts(self):
        d = get("http://127.0.0.1:5008/api/dash") or {}
        self.header("Alertes")
        alerts = (d.get("alerts") or [])[:8]
        if not alerts:
            self.row(0, "", "Aucune alerte recente", GREEN)
        for i, al in enumerate(alerts):
            self.row(i, time.strftime("%d/%m %H:%M", time.localtime(al["time"])), al.get("title", "")[:30],
                     RED if time.time() - al["time"] < 3600 else WHITE)

    def draw_sats(self):
        st = get("http://127.0.0.1:5013/api/meteor/status") or {}
        self.header("Satellites")
        passes = [p for p in st.get("passes") or [] if p.get("wanted")][:7]
        if not st:
            self.row(0, "", "module satellites absent", GRAY)
        elif not passes:
            self.row(0, "", "aucun passage retenu", GRAY)
        days = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")
        for i, p in enumerate(passes):
            lt = time.localtime(p["aos"])
            name = "ISS (SSTV)" if p.get("kind") == "sstv" else p["sat"].replace("Meteor-M ", "Meteor ")
            self.row(i, f"{days[lt.tm_wday]} {time.strftime('%H:%M', lt)}", f"{name} - {p['max_el']:.0f} deg",
                     CYAN if p.get("kind") == "sstv" else WHITE)
        cur = st.get("current")
        if cur:
            self.row(7, "En cours", cur.get("sat", ""), ORANGE)

    def loop(self):
        screens = (self.draw_station, self.draw_alerts, self.draw_sats)
        last_switch = time.time()
        while True:
            try:
                self.cmd("page 0")
                screens[self.page % len(screens)]()
            except serial.SerialException as exc:
                log.warning("Écran : %s", exc)
            # Rafraîchit l'écran courant toutes les 10 s ; change d'écran sur appui ou au bout de cycle_s
            if self.touched.wait(10):
                self.touched.clear()
                self.page += 1
                last_switch = time.time()
            elif conf.get("cycle_s") and time.time() - last_switch >= float(conf["cycle_s"]):
                self.page += 1
                last_switch = time.time()


if __name__ == "__main__":
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            conf.update(json.load(f))
    except (OSError, ValueError):
        pass
    scr = Screen()
    scr.setup()
    threading.Thread(target=scr.listen_touch, daemon=True).start()
    log.info("Écran Nextion sur %s à %s bauds", conf["port"], conf["baud"])
    scr.loop()
