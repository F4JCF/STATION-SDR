#!/usr/bin/env python3
"""
Décodeur SSTV (famille PD : PD90, PD120, PD160, PD180, PD240, PD290), celle
qu'emploie l'ISS pendant les événements ARISS.

Entrée : enregistrement audio mono (s16le brut ou WAV) issu de rtl_fm.
Sortie : une image PNG par image reçue (complète ou partielle).

Principe :
  1. fréquence instantanée du signal (signal analytique, bande 900-2600 Hz) ;
  2. recherche des en-têtes VIS (mode et début exact de l'image) ;
  3. recherche des tops de synchro (1200 Hz, 20 ms), regroupés en suites
     régulières dont la période identifie le mode — une image prise en cours
     (début de passage) est donc décodée aussi, sans en-tête ;
  4. ajustement linéaire des tops (corrige la dérive d'horloge), puis lecture
     des pixels Y / R-Y / B-Y / Y.

Usage : sstv_decode.py fichier.raw|fichier.wav [--rate 11025] [--out dossier] [--prefix nom]
"""

import argparse
import json
import os
import sys
import wave

import numpy as np

# vis : (nom, largeur, hauteur, durée d'un pixel en ms)
MODES = {
    99: ("PD90", 320, 256, 0.532),
    95: ("PD120", 640, 496, 0.190),
    98: ("PD160", 512, 400, 0.382),
    96: ("PD180", 640, 496, 0.286),
    97: ("PD240", 640, 496, 0.382),
    94: ("PD290", 800, 616, 0.286),
}
SYNC_MS = 20.0
PORCH_MS = 2.08
MIN_PAIRS = 12            # en dessous : bruit, pas d'image


def line_ms(vis):
    _n, w, _h, px = MODES[vis]
    return SYNC_MS + PORCH_MS + 4 * w * px


# -------------------------------------------------------------- audio --

def load_audio(path, rate=None):
    if path.lower().endswith(".wav"):
        with wave.open(path, "rb") as w:
            fs = w.getframerate()
            ch = w.getnchannels()
            raw = w.readframes(w.getnframes())
            width = w.getsampwidth()
        dt = {1: np.uint8, 2: np.int16, 4: np.int32}[width]
        x = np.frombuffer(raw, dtype=dt).astype(np.float32)
        if width == 1:
            x -= 128
        if ch > 1:
            x = x.reshape(-1, ch)[:, 0]
        return x, fs
    x = np.fromfile(path, dtype="<i2").astype(np.float32)
    return x, rate or 11025


def inst_freq(x, fs, block=1 << 16, pad=2048, lo=900.0, hi=2600.0):
    """Fréquence instantanée (Hz) échantillon par échantillon."""
    n = len(x)
    out = np.empty(n, np.float32)
    for start in range(0, n, block):
        a, b = max(0, start - pad), min(n, start + block + pad)
        seg = x[a:b].astype(np.float64)
        m = len(seg)
        size = 1 << max(1, (m - 1).bit_length())
        spec = np.fft.fft(seg, size)
        freqs = np.fft.fftfreq(size, 1.0 / fs)
        h = np.where((freqs >= lo) & (freqs <= hi), 2.0, 0.0)   # analytique + passe-bande
        z = np.fft.ifft(spec * h)[:m]
        d = np.angle(z[1:] * np.conj(z[:-1])) * fs / (2 * np.pi)
        f = np.empty(m)
        f[1:] = d
        f[0] = d[0] if len(d) else 0
        end = min(start + block, n)
        out[start:end] = f[start - a:start - a + end - start]
    return out


def boxcar(v, k):
    """Moyenne glissante centrée sur k échantillons (mémoire réduite : Pi 3)."""
    n = len(v)
    k = max(1, min(int(k), n))
    c = np.empty(n + 1, np.float64)
    c[0] = 0.0
    np.cumsum(v, dtype=np.float64, out=c[1:])
    out = np.empty(n, np.float32)
    half = k // 2
    core = (c[k:] - c[:-k]) / k
    out[half:half + len(core)] = core
    out[:half] = core[0]
    out[half + len(core):] = core[-1]
    return out


# ---------------------------------------------------------------- VIS --

def find_vis(f, fs):
    """Liste de (vis, fin_vis_en_s) pour chaque en-tête valide."""
    ms = fs / 1000.0
    k30 = int(round(30 * ms))
    m30 = boxcar(f, k30)               # moyenne sur 30 ms centrée
    lead = boxcar(np.abs(f - 1900) < 80, int(250 * ms))
    found = []
    # début candidat du bit de départ : 30 ms vers 1200 Hz précédés de ~250 ms à 1900 Hz
    step = max(1, int(ms))
    cand = np.arange(int(260 * ms), len(f) - int(320 * ms), step)
    centre = cand + k30 // 2
    ok = (np.abs(m30[centre] - 1200) < 60) & (lead[cand - int(130 * ms)] > 0.7)
    cand = cand[ok]
    if not len(cand):
        return found
    groups = np.split(cand, np.where(np.diff(cand) > 3 * step)[0] + 1)
    for g in groups:
        best = None
        for t in range(int(g[0] - 10 * ms), int(g[-1] + 10 * ms), step):
            slots = [m30[min(len(f) - 1, t + int((30 * i + 15) * ms))] for i in range(10)]
            bits = [1 if s < 1200 else 0 for s in slots[1:9]]
            err = (slots[0] - 1200) ** 2 + (slots[9] - 1200) ** 2 + sum(
                min((s - 1100) ** 2, (s - 1300) ** 2) for s in slots[1:9])
            if best is None or err < best[0]:
                best = (err, t, bits)
        err, t, bits = best
        if err / 10 > 70 ** 2:
            continue
        code = sum(b << i for i, b in enumerate(bits[:7]))
        if sum(bits[:7]) % 2 != bits[7] or code not in MODES:
            continue
        end = (t + 300 * ms) / fs
        if not found or end - found[-1][1] > 1.0:
            found.append((code, end))
    return found


# ------------------------------------------------------------- synchro --

def find_syncs(f, fs):
    """Centres (s) des tops de synchro de 20 ms à 1200 Hz."""
    ms = fs / 1000.0
    ind = ((f > 1050) & (f < 1350)).astype(np.float32)
    conv = boxcar(ind, int(round(SYNC_MS * ms)))
    above = conv > 0.6
    edges = np.diff(above.astype(np.int8))
    starts = list(np.where(edges == 1)[0] + 1)
    ends = list(np.where(edges == -1)[0] + 1)
    if above[0]:
        starts.insert(0, 0)
    if above[-1]:
        ends.append(len(f))
    out = []
    for a, b in zip(starts, ends):
        length = (b - a) / ms
        if length < 4:
            continue
        if length <= 22:
            w = conv[a:b] - 0.6
            c = a + float(np.sum(w * np.arange(b - a)) / np.sum(w))
        else:                              # top prolongé (bit d'arrêt VIS) : on se cale sur la fin
            c = b - 8 * ms
        out.append(c / fs)
    return np.array(out)


def chains_for(pulses, period, max_step=4, tol=0.003):
    """Suites de tops espacés d'un petit multiple de la période (s), puis
    recollées entre elles si elles tombent sur la même grille (évanouissement)."""
    n = len(pulses)
    parent = list(range(n))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        j = i + 1
        while j < n and pulses[j] - pulses[i] < max_step * period + tol:
            k = round((pulses[j] - pulses[i]) / period)
            if k >= 1 and abs(pulses[j] - pulses[i] - k * period) < tol:
                parent[root(j)] = root(i)
            j += 1
    groups = {}
    for i in range(n):
        groups.setdefault(root(i), []).append(i)
    chains = []
    for idx in groups.values():
        if len(idx) < 3:
            continue
        ts = pulses[sorted(idx)]
        ks = [0]
        for d in np.diff(ts):
            ks.append(ks[-1] + max(1, int(round(d / period))))
        chains.append([(k, t) for k, t in zip(ks, ts)])
    chains.sort(key=lambda c: c[0][1])
    # Recollage : même grille, trou de moins de 60 lignes
    merged = []
    for ch in chains:
        if merged:
            prev = merged[-1]
            a, b, _g = fit(prev) if len(prev) >= 4 else (prev[0][1], period, 0)
            gap = ch[0][1] - prev[-1][1]
            k = (ch[0][1] - a) / b
            if gap < 60 * period and abs(k - round(k)) * b < 0.006 + 0.0001 * gap:
                off = int(round(k)) - ch[0][0]
                merged[-1] = prev + [(c[0] + off, c[1]) for c in ch]
                continue
        merged.append(ch)
    return [c for c in merged if len(c) >= MIN_PAIRS // 2 and len(c) / (c[-1][0] + 1) > 0.3]


def fit(chain):
    idx = np.array([c[0] for c in chain], float)
    t = np.array([c[1] for c in chain], float)
    keep = np.ones(len(t), bool)
    for _ in range(3):
        b, a = np.polyfit(idx[keep], t[keep], 1)
        res = np.abs(t - (a + b * idx))
        lim = max(0.002, 3 * np.median(res[keep]))
        keep = res < lim
        if keep.sum() < 4:
            break
    return a, b, int(keep.sum())


# --------------------------------------------------------------- image --

def render(f, fs, vis, a, b, first, count):
    """Lit `count` paires de lignes, la paire i ayant son top centré à a + b*i."""
    from PIL import Image
    name, w, h, px = MODES[vis]
    scale = b / (line_ms(vis) / 1000.0)
    c = np.concatenate(([0.0], np.cumsum(np.clip(f, 1500, 2300) - 1500, dtype=np.float64)))
    pairs = np.arange(first, first + count)
    sync_start = a + b * pairs - SYNC_MS / 2000.0 * scale
    data0 = sync_start + (SYNC_MS + PORCH_MS) / 1000.0 * scale
    edges = data0[:, None] + np.arange(4 * w + 1)[None, :] * (px / 1000.0 * scale)
    pos = np.clip(edges * fs, 0, len(f) - 1)
    cum = np.interp(pos, np.arange(len(c)), c)
    widths = np.diff(pos, axis=1)
    vals = np.diff(cum, axis=1) / np.maximum(widths, 1e-9)
    vals = np.clip(vals / 800.0 * 255.0, 0, 255)
    vals[widths[:, 0] < 0.5] = 0                      # hors de l'enregistrement
    y0, cr, cb, y1 = (vals[:, i * w:(i + 1) * w] for i in range(4))
    rows = np.empty((2 * count, w, 3), np.uint8)
    rows[0::2, :, 0] = y0
    rows[1::2, :, 0] = y1
    rows[0::2, :, 1] = rows[1::2, :, 1] = cb
    rows[0::2, :, 2] = rows[1::2, :, 2] = cr
    full = np.zeros((h, w, 3), np.uint8)
    full[:, :, 1:] = 128
    top = 2 * (first - min(first, 0))
    full[top:top + 2 * count] = rows[: h - top]
    return Image.fromarray(full, "YCbCr").convert("RGB")


def decode(x, fs):
    """Renvoie une liste de dict {image, mode, start, complete, lines}."""
    f = inst_freq(x, fs)
    vis_list = find_vis(f, fs)
    pulses = find_syncs(f, fs)
    cands = []
    for vis in MODES:
        period = line_ms(vis) / 1000.0
        for ch in chains_for(pulses, period):
            a, b, good = fit(ch)
            if abs(b / period - 1) > 0.003 or good < MIN_PAIRS // 2:
                continue
            cands.append((good, vis, a, b, ch[-1][0]))
    # Garde les meilleures suites qui ne se chevauchent pas
    cands.sort(key=lambda c: -c[0])
    chosen = []
    for good, vis, a, b, last in cands:
        t0, t1 = a, a + b * last
        if any(not (t1 < o0 - 0.5 or t0 > o1 + 0.5) for o0, o1, *_ in chosen):
            continue
        chosen.append((t0, t1, good, vis, a, b, last))
    results = []
    for t0, t1, good, vis, a, b, last in sorted(chosen):
        _n, w, h, _px = MODES[vis]
        npairs = h // 2
        offset, complete = 0, False
        for v, vend in vis_list:          # en-tête VIS juste avant la suite : début exact
            if v != vis:
                continue
            k = (a - b / (line_ms(vis) / 1000.0) * SYNC_MS / 2000.0 - vend) / b
            kr = int(round(k))
            if -1 <= kr < npairs and abs(k - kr) * b < 0.012:
                offset, complete = kr, True
                break
        # La paire 0 de l'image commence à a + b*(-offset)
        a_img = a - b * offset
        count = min(npairs, last + offset + 1)
        if count < MIN_PAIRS:
            continue
        img = render(f, fs, vis, a_img, b, 0, count)
        if count < npairs:
            img = img.crop((0, 0, w, 2 * count))
        results.append({"image": img, "mode": MODES[vis][0], "start": a_img, "complete": complete and count == npairs,
                        "lines": 2 * count, "quality": round(good / count, 2)})
        # Une suite plus longue qu'une image : la suite (sans VIS détecté) est une autre image
        rest = last + offset + 1 - npairs
        if rest >= MIN_PAIRS:
            img2 = render(f, fs, vis, a_img + b * npairs, b, 0, min(npairs, rest))
            if rest < npairs:
                img2 = img2.crop((0, 0, w, 2 * rest))
            results.append({"image": img2, "mode": MODES[vis][0], "start": a_img + b * npairs, "complete": False,
                            "lines": 2 * min(npairs, rest), "quality": round(good / count, 2)})
    return results


def main():
    ap = argparse.ArgumentParser(description="Décodeur SSTV (modes PD)")
    ap.add_argument("audio")
    ap.add_argument("--rate", type=int, default=11025)
    ap.add_argument("--out", default=".")
    ap.add_argument("--prefix", default="sstv")
    args = ap.parse_args()
    x, fs = load_audio(args.audio, args.rate)
    res = decode(x, fs)
    os.makedirs(args.out, exist_ok=True)
    summary = []
    for i, r in enumerate(res, 1):
        name = f"{args.prefix}-{i}" if len(res) > 1 else args.prefix
        path = os.path.join(args.out, name + ".png")
        r["image"].save(path)
        summary.append({"file": path, "mode": r["mode"], "offset_s": round(r["start"], 2),
                        "complete": r["complete"], "lines": r["lines"], "quality": r["quality"]})
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
