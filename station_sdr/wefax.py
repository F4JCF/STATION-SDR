"""
Décodeur WEFAX (radiofac-similé météo HF : DWD Hambourg, Northwood…).

Le récepteur est réglé en BLU haute (USB) 1,9 kHz sous la fréquence publiée :
la porteuse audio est alors à 1900 Hz, noir = 1500 Hz, blanc = 2300 Hz.
Une carte commence par une tonalité de départ (300 Hz pour l'IOC 576), puis
30 s de phasage (lignes avec un top marquant le début de ligne), puis l'image
(120 lignes par minute en général), et finit par une tonalité d'arrêt (450 Hz).

Utilisable en direct (WefaxDecoder.feed, morceau par morceau) ou sur un
fichier (decode_file). Dépend uniquement de numpy (et de PIL pour les PNG).
Ce fichier sert aussi tel quel sur le Pi (/usr/local/bin/wefax_decode.py).
"""

import math
import time

import numpy as np

CARRIER = 1900.0
SHIFT = 400.0             # ±400 Hz autour de la porteuse
WIDTH_576 = 1810          # pixels par ligne pour l'IOC 576 (π × 576)
MAX_LINES = 2600


class WefaxDecoder:
    """Décodage en continu. Rappels : on_start(info), on_line(n, row uint8),
    on_end(image uint8 2D, info)."""

    def __init__(self, fs, lpm=120, ppm=0.0, on_start=None, on_line=None, on_end=None, auto_start=True):
        self.fs = float(fs)
        self.lpm = lpm
        self.ppm = float(ppm)
        self.width = WIDTH_576
        self.on_start = on_start or (lambda info: None)
        self.on_line = on_line or (lambda n, row: None)
        self.on_end = on_end or (lambda img, info: None)
        self.auto_start = auto_start
        # Démodulation FM (mélange à la porteuse, filtre, discriminateur)
        self._n = 0
        ntap = int(self.fs / 1000) * 6 + 1
        t = np.arange(ntap) - ntap // 2
        fc = 1100.0 / self.fs
        h = np.sinc(2 * fc * t) * np.hamming(ntap)
        self._h = (h / h.sum()).astype(np.float64)
        self._ztail = np.zeros(ntap - 1, complex)
        self._zlast = 1 + 0j
        # Luminance accumulée (0 = noir, 1 = blanc) et puissance du signal
        self._lum = np.zeros(0, np.float32)
        self._lum_t0 = 0          # indice absolu du premier échantillon de _lum
        self._sig = []            # (indice, puissance utile / totale) par bloc
        self.state = "idle"       # idle | start | phasing | image
        self.rows = []
        self.info = {}
        self._line_pos = None     # indice absolu (flottant) du début de la ligne suivante
        self._phase_marks = []
        self._tone_run = {"start": 0, "stop": 0}
        self._quiet = 0
        self.level = 0.0

    # ------------------------------------------------------------ outils
    @property
    def line_len(self):
        return self.fs * 60.0 / self.lpm * (1 + self.ppm * 1e-6)

    def _demod(self, x):
        n = np.arange(self._n, self._n + len(x))
        self._n += len(x)
        z = x.astype(np.float64) * np.exp(-2j * math.pi * CARRIER * n / self.fs)
        zz = np.concatenate((self._ztail, z))
        y = np.convolve(zz, self._h, mode="valid")
        self._ztail = zz[-(len(self._h) - 1):]
        prev = np.concatenate(([self._zlast], y[:-1]))
        self._zlast = y[-1] if len(y) else self._zlast
        df = np.angle(y * np.conj(prev)) * self.fs / (2 * math.pi)
        lum = np.clip((df + SHIFT) / (2 * SHIFT), 0, 1).astype(np.float32)
        # Qualité : part de l'énergie dans la bande WEFAX
        p_in = float(np.mean(np.abs(y) ** 2))
        p_all = float(np.mean(x.astype(np.float64) ** 2)) + 1e-9
        return lum, min(1.0, p_in / p_all * 2)

    def _tone(self, seg):
        """Fréquence dominante de la luminance (tonalités de départ / d'arrêt)."""
        if len(seg) < 256:
            return None, 0
        s = seg - seg.mean()
        spec = np.abs(np.fft.rfft(s * np.hanning(len(s))))
        freqs = np.fft.rfftfreq(len(s), 1 / self.fs)
        band = (freqs > 150) & (freqs < 900)
        if not band.any():
            return None, 0
        i = np.argmax(spec * band)
        ratio = spec[i] / (np.sum(spec[band]) / band.sum() + 1e-9)
        return freqs[i], ratio

    # ------------------------------------------------------------- flux
    def feed(self, x):
        lum, q = self._demod(np.asarray(x, dtype=np.float32))
        self.level = 0.8 * self.level + 0.2 * q
        self._lum = np.concatenate((self._lum, lum))
        self._process()

    def _process(self):
        L = self.line_len
        while True:
            avail_end = self._lum_t0 + len(self._lum)
            if self.state in ("idle", "start"):
                # On examine des tranches de 0,25 s pour les tonalités
                win = int(self.fs * 0.25)
                if len(self._lum) < win:
                    return
                seg = self._lum[:win]
                f, ratio = self._tone(seg)
                is_start = f is not None and ratio > 12 and (abs(f - 300) < 25 or abs(f - 675) < 30)
                if is_start:
                    self._tone_run["start"] += 1
                    if self._tone_run["start"] >= 6 and self.state == "idle":
                        self.state = "start"
                        self.info = {"ioc": 288 if abs(f - 675) < 30 else 576, "start": time.time(),
                                     "header": True}
                elif self.state == "start":
                    # fin de la tonalité de départ : le phasage commence
                    self.state = "phasing"
                    self._line_pos = float(self._lum_t0)
                    self._phase_marks = []
                    self.on_start(dict(self.info))
                else:
                    self._tone_run["start"] = 0
                    # Image sans en-tête (début de carte manqué) : signal présent et contrasté
                    if self.auto_start and self.level > 0.55 and float(seg.std()) > 0.18:
                        self._quiet += 1
                        if self._quiet > 40:            # ~10 s de signal d'image
                            self.state = "image"
                            self.info = {"ioc": 576, "start": time.time(), "header": False}
                            self._line_pos = float(self._lum_t0)
                            self.rows = []
                            self.on_start(dict(self.info))
                            self._quiet = 0
                            continue
                    else:
                        self._quiet = 0
                self._lum = self._lum[win:]
                self._lum_t0 += win
                continue
            # Phasage ou image : ligne par ligne
            if self._line_pos + L + 2 > avail_end:
                return
            a = int(self._line_pos) - self._lum_t0
            b = int(self._line_pos + L) - self._lum_t0
            line = self._lum[max(0, a):b]
            row = self._resample(line)
            f, ratio = self._tone(line)
            stop = f is not None and ratio > 12 and abs(f - 450) < 30
            self._tone_run["stop"] = self._tone_run["stop"] + 1 if stop else 0
            if self.state == "phasing":
                kind = self._phasing_mark(row)
                if kind is not None:
                    self._phase_marks.append((self._line_count(), kind))
                    self._miss = 0
                else:
                    self._miss = getattr(self, "_miss", 0) + 1
                    if (len(self._phase_marks) >= 5 and self._miss >= 3) or \
                            (not self._phase_marks and self._line_count() > 80):
                        # les lignes manquées font déjà partie de l'image
                        self._line_pos -= (self._miss - 1) * self.line_len
                        self._finish_phasing()
                        continue
                self._advance()
                if self._line_count() > 120:     # phasage interminable : on passe à l'image
                    self._finish_phasing()
                continue
            # Image
            if self._tone_run["stop"] >= 4 or len(self.rows) >= MAX_LINES:
                self._end()
                continue
            if self.level < 0.2:
                self._tone_run["quiet"] = self._tone_run.get("quiet", 0) + 1
                if self._tone_run["quiet"] > 40:      # 20 s sans signal
                    self._end()
                    continue
            else:
                self._tone_run["quiet"] = 0
            self.rows.append(row)
            self.on_line(len(self.rows) - 1, row)
            self._advance()

    def _line_count(self):
        return getattr(self, "_lc", 0)

    def _advance(self):
        self._lc = self._line_count() + 1
        self._line_pos += self.line_len
        drop = int(self._line_pos) - self._lum_t0 - int(self.fs)     # garde 1 s de marge
        if drop > 0:
            self._lum = self._lum[drop:]
            self._lum_t0 += drop

    def _resample(self, line):
        if len(line) < 10:
            return np.zeros(self.width, np.uint8)
        c = np.concatenate(([0.0], np.cumsum(line, dtype=np.float64)))
        edges = np.linspace(0, len(line), self.width + 1)
        cum = np.interp(edges, np.arange(len(c)), c)
        vals = np.diff(cum) / np.maximum(np.diff(edges), 1e-9)
        return np.clip(vals * 255, 0, 255).astype(np.uint8)

    def _phasing_mark(self, row):
        """Ligne de phasage : uniforme, avec un seul top court (environ 5 % de
        la ligne). Renvoie la position du milieu du top, ou None."""
        r = row.astype(np.float32) / 255
        med = float(np.median(r))
        m = (np.abs(r - med) > 0.5).astype(np.int8)
        frac = float(m.mean())
        if not 0.025 <= frac <= 0.09:
            return None
        # un seul segment (en tenant compte du bouclage en fin de ligne)
        shift = int(np.argmin(m)) if m.any() and not m.all() else 0
        mm = np.roll(m, -shift)
        edges = np.flatnonzero(np.diff(np.concatenate(([0], mm, [0]))))
        runs = list(zip(edges[::2], edges[1::2]))
        big = [(a, b) for a, b in runs if b - a > 3]
        if len(big) != 1 or sum(b - a for a, b in runs) > 1.3 * (big[0][1] - big[0][0]):
            return None
        centre = ((big[0][0] + big[0][1]) / 2 + shift) % self.width
        if self._phase_marks:
            prev = self._phase_marks[-1][1]
            d = abs(centre - prev)
            if min(d, self.width - d) > 40:
                return None
        return centre

    def _finish_phasing(self):
        marks = self._phase_marks
        if len(marks) >= 5:
            idx = np.array([m[0] for m in marks], float)
            pos = np.unwrap(np.array([m[1] for m in marks], float) / self.width * 2 * np.pi) / (2 * np.pi) * self.width
            # pente robuste (médiane des pentes entre points éloignés), puis ordonnée médiane
            n = len(idx)
            pairs = [(pos[j] - pos[i]) / (idx[j] - idx[i]) for i in range(n) for j in range(i + n // 3, n)
                     if idx[j] > idx[i]]
            slope = float(np.median(pairs)) if pairs else 0.0
            inter = float(np.median(pos - slope * idx))
            # dérive d'horloge : slope pixels par ligne
            self.ppm += slope / self.width * 1e6
            self.info["ppm"] = round(self.ppm, 1)
            # début de ligne = début du top de phasage (marge gauche de la carte)
            last = inter + slope * self._line_count()
            shift = (last - self.width * 0.025) % self.width
            self._line_pos += shift / self.width * self.line_len
        self.state = "image"
        self.rows = []
        self._miss = 0

    def _end(self):
        img = np.array(self.rows, np.uint8) if self.rows else None
        info = dict(self.info)
        info["lines"] = len(self.rows)
        info["end"] = time.time()
        self.state = "idle"
        self.rows = []
        self._lc = 0
        self._tone_run = {"start": 0, "stop": 0}
        if img is not None and len(img) >= 50:
            self.on_end(img, info)

    def flush(self):
        """Termine l'image en cours (fin d'enregistrement)."""
        if self.state == "image" and self.rows:
            self._end()


def decode_file(x, fs, ppm=0.0):
    """Décode un enregistrement complet ; renvoie [(image uint8, info), …]."""
    out = []
    dec = WefaxDecoder(fs, ppm=ppm, on_end=lambda img, info: out.append((img, info)))
    step = int(fs)
    for i in range(0, len(x), step):
        dec.feed(x[i:i + step])
    dec.flush()
    return out


def save_png(img, path, max_width=1810):
    from PIL import Image
    im = Image.fromarray(img, "L")
    if im.width > max_width:
        im = im.resize((max_width, int(im.height * max_width / im.width)))
    im.save(path)
    return im


if __name__ == "__main__":
    import argparse
    import json
    import os
    import sys
    ap = argparse.ArgumentParser(description="Décodeur WEFAX")
    ap.add_argument("audio", help="s16le brut")
    ap.add_argument("--rate", type=int, default=12000)
    ap.add_argument("--out", default=".")
    ap.add_argument("--prefix", default="wefax")
    a = ap.parse_args()
    data = np.fromfile(a.audio, dtype="<i2").astype(np.float32)
    res = decode_file(data, a.rate)
    os.makedirs(a.out, exist_ok=True)
    summ = []
    for i, (img, info) in enumerate(res, 1):
        p = os.path.join(a.out, f"{a.prefix}-{i}.png")
        save_png(img, p)
        summ.append({"file": p, **{k: v for k, v in info.items() if k in ("lines", "header", "ppm", "ioc")}})
    json.dump(summ, sys.stdout)
    print()
