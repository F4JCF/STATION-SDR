#!/usr/bin/env python3
"""Tâche satellite lancée par la bascule de clé (mode « meteor ») :
  - « lrpt »  : passage Meteor-M, rtl_sdr → meteor_demod → meteor_decode ;
  - « sstv »  : passage de l'ISS, audio FM 145,800 MHz enregistré puis décodé
                (sstv_decode.py) au coucher ;
  - « noise » : relevé du bruit radio (rtl_power) sur quelques bandes.
Les images (PNG, vignette, fiche) vont dans /var/lib/meteor/images, les relevés
dans /var/lib/meteor/noise.json. Ensuite le service reste au repos jusqu'à la
prochaine bascule (le chien de garde ne le considère pas comme tombé)."""

import json
import logging
import os
import shutil
import signal
import subprocess
import time

JOB_FILE = "/run/meteor/job.json"
RATE = 230000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("meteor-rx")


def finish(result: dict) -> None:
    with open(JOB_FILE + ".done", "w", encoding="utf-8") as f:
        json.dump(result, f)


def post_process(job, png, extra=None):
    from PIL import Image
    img = Image.open(png)
    w, h = img.size
    if h < 64:
        return None
    if job.get("northbound") and job.get("kind", "lrpt") == "lrpt":
        img = img.rotate(180)
        img.save(png)
    useful = None
    if job.get("kind", "lrpt") == "lrpt":
        # Part des lignes réellement reçues (le décodeur met du noir à la place des paquets perdus)
        gray = img.convert("L")
        gw, gh = gray.size
        data = gray.tobytes()
        rows = sum(1 for y in range(gh) if sum(data[y * gw:(y + 1) * gw:8]) > 12 * len(range(0, gw, 8)))
        useful = round(100 * rows / gh)
        if useful < 3:
            return None
    thumb = img.convert("RGB")
    thumb.thumbnail((480, 900))
    thumb.save(png[:-4] + ".jpg", quality=82)
    meta = {"name": job["name"], "sat": job["sat"], "aos": job["aos"], "max_el": job["max_el"],
            "width": w, "lines": h, "size": os.path.getsize(png), "time": time.time(),
            "kind": job.get("kind", "lrpt"), "useful_pct": useful, **(extra or {})}
    with open(png[:-4] + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f)
    return meta


def receive_sstv(job):
    """Enregistre l'audio FM de l'ISS jusqu'au coucher, puis décode."""
    if not shutil.which("rtl_fm"):
        finish({"result": "rtl_fm absent"})
        return
    raw = "/run/meteor/sstv.raw"
    rate = 11025
    rec_start = time.time()
    with open(raw, "wb") as out:
        p = subprocess.Popen(["rtl_fm", "-M", "fm", "-f", str(int(job["freq_mhz"] * 1e6)), "-s", "24000",
                              "-r", str(rate), "-F", "9", "-g", str(job["gain"]), "-"],
                             stdout=out, stderr=subprocess.DEVNULL)
        log.info("Écoute SSTV %s jusqu'à %s", job["sat"], time.strftime("%H:%M:%S", time.localtime(job["los"])))
        while time.time() < job["los"] and p.poll() is None:
            time.sleep(2)
        if p.poll() is None:
            p.send_signal(signal.SIGINT)
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import sstv_decode
        x, fs = sstv_decode.load_audio(raw, rate)
        if len(x) < rate * 10:
            finish({"result": "enregistrement trop court"})
            return
        results = sstv_decode.decode(x, fs)
    except ImportError as exc:
        finish({"result": f"décodeur SSTV absent ({exc}) : mettre à jour le Pi"})
        return
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass
    os.makedirs(job["out_dir"], exist_ok=True)
    names = []
    for i, r in enumerate(results, 1):
        name = job["name"] + (f"-{i}" if len(results) > 1 else "")
        png = os.path.join(job["out_dir"], name + ".png")
        r["image"].save(png)
        post_process({**job, "name": name}, png, {"mode": r["mode"], "complete": r["complete"],
                                                    "quality": r["quality"],
                                                    "rx_time": round(rec_start + r["start"])})
        names.append(name)
    if not names:
        finish({"result": "aucune image SSTV (pas d'émission ou signal trop faible)"})
        return
    finish({"result": f"{len(names)} image(s) SSTV", "name": names[0]})
    log.info("SSTV : %d image(s)", len(names))


NOISE_FILE = "/var/lib/meteor/noise.json"
NOISE_REF_FILE = "/var/lib/meteor/noise_ref.json"


def receive_noise(job):
    """Relevé du bruit : rtl_power une fois par bande, médiane et 90e centile (dB)."""
    if not shutil.which("rtl_power"):
        finish({"result": "rtl_power absent"})
        return
    entry = {"time": time.time(), "gain": job["gain"], "bands": {}}
    if job.get("manual"):
        entry["manual"] = True             # mesure lancée à la main (hors référence nocturne)
    if job.get("reference"):
        entry["reference"] = True          # antenne remplacée par une charge 50 Ω : bruit propre de la clé
    for name, lo, hi in job["bands"]:
        span = hi - lo
        step = 10000 if span <= 5 else 25000
        try:
            out = subprocess.run(["rtl_power", "-f", f"{lo}M:{hi}M:{step}", "-i", "8", "-1",
                                  "-g", str(job["gain"]), "-"], capture_output=True, text=True, timeout=120).stdout
        except subprocess.TimeoutExpired:
            continue
        vals = []
        for line in out.splitlines():
            parts = line.split(",")
            if len(parts) > 6:
                for v in parts[6:]:
                    try:
                        x = float(v)
                    except ValueError:
                        continue
                    if x == x and abs(x) < 200:
                        vals.append(x)
        if vals:
            vals.sort()
            entry["bands"][name] = {"median": round(vals[len(vals) // 2], 2),
                                    "p90": round(vals[int(len(vals) * 0.9)], 2),
                                    "max": round(vals[-1], 2)}
    path = NOISE_REF_FILE if job.get("reference") else NOISE_FILE
    try:
        with open(path, encoding="utf-8") as f:
            hist = json.load(f)
    except (OSError, ValueError):
        hist = []
    if entry["bands"]:
        hist.append(entry)
        tmp = path + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist[-(10 if job.get("reference") else 500):], f)
        os.replace(tmp, path)
    what = "référence 50 Ω" if job.get("reference") else "bruit"
    finish({"result": f"{what} mesuré sur {len(entry['bands'])} bande(s)"})


def receive_wefax(job):
    """WEFAX par la clé + convertisseur HF : BLU haute 1,9 kHz sous la fréquence publiée."""
    if not shutil.which("rtl_fm"):
        finish({"result": "rtl_fm absent"})
        return
    raw = "/run/meteor/wefax.raw"
    rate = 12000
    hz = int(job["khz"] * 1000 - 1900 + job.get("offset_mhz", 0) * 1e6)
    with open(raw, "wb") as out:
        p = subprocess.Popen(["rtl_fm", "-M", "usb", "-f", str(hz), "-s", str(rate), "-g", str(job["gain"]), "-"],
                             stdout=out, stderr=subprocess.DEVNULL)
        log.info("WEFAX %.1f kHz jusqu'à %s", job["khz"], time.strftime("%H:%M", time.localtime(job["los"])))
        while time.time() < job["los"] and p.poll() is None:
            time.sleep(2)
        if p.poll() is None:
            p.send_signal(signal.SIGINT)
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
    try:
        import sys
        import numpy as np
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import wefax_decode
        x = np.fromfile(raw, dtype="<i2").astype(np.float32)
        results = wefax_decode.decode_file(x, rate)
    except ImportError as exc:
        finish({"result": f"décodeur WEFAX absent ({exc}) : mettre à jour le Pi"})
        return
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass
    os.makedirs(job["out_dir"], exist_ok=True)
    names = []
    for i, (img, info) in enumerate(results, 1):
        name = job["name"] + (f"-{i}" if len(results) > 1 else "")
        png = os.path.join(job["out_dir"], name + ".png")
        wefax_decode.save_png(img, png)
        post_process({**job, "name": name}, png, {"khz": job["khz"], "complete": bool(info.get("header")),
                                                    "ppm": info.get("ppm")})
        names.append(name)
    finish({"result": f"{len(names)} carte(s) WEFAX" if names else "aucune carte WEFAX (pas de signal)",
            **({"name": names[0]} if names else {})})


def receive(job):
    kind = job.get("kind", "lrpt")
    if kind == "wefax":
        receive_wefax(job)
        return
    if kind == "sstv":
        receive_sstv(job)
        return
    if kind == "noise":
        receive_noise(job)
        return
    os.makedirs(job["out_dir"], exist_ok=True)
    tmp_png = f"/run/meteor/{job['name']}.png"
    for tool in ("rtl_sdr", "meteor_demod", "meteor_decode"):
        if not shutil.which(tool):
            finish({"result": f"{tool} absent : réinstaller le module Meteor"})
            return
    p1 = subprocess.Popen(["rtl_sdr", "-f", str(int(job["freq_mhz"] * 1e6)), "-s", str(RATE),
                           "-g", str(job["gain"]), "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    p2 = subprocess.Popen(["meteor_demod", "--bps", "8", "-s", str(RATE), "-m", job["mode"], "-r",
                           str(job["symrate"]), "--stdout", "-"], stdin=p1.stdout, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
    p1.stdout.close()
    cmd3 = ["meteor_decode", "-B", "-q"] + (["-d"] if job.get("diff") else []) + ["-o", tmp_png, "-"]
    p3 = subprocess.Popen(cmd3, stdin=p2.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p2.stdout.close()
    log.info("Réception de %s jusqu'à %s", job["sat"], time.strftime("%H:%M:%S", time.localtime(job["los"])))
    while time.time() < job["los"] and p1.poll() is None:
        time.sleep(2)
    if p1.poll() is None:
        p1.send_signal(signal.SIGINT)
    for p in (p1, p2, p3):
        try:
            p.wait(timeout=180)
        except subprocess.TimeoutExpired:
            p.kill()
    if not os.path.exists(tmp_png):
        finish({"result": "aucune image (signal trop faible ou satellite en panne)"})
        return
    final = os.path.join(job["out_dir"], job["name"] + ".png")
    shutil.move(tmp_png, final)
    try:
        meta = post_process(job, final)
    except Exception as exc:
        meta = None
        log.warning("Post-traitement : %s", exc)
    if not meta:
        for ext in (".png", ".jpg", ".json"):
            try:
                os.remove(final[:-4] + ext)
            except OSError:
                pass
        finish({"result": "image vide ou trop courte (signal trop faible)"})
        return
    finish({"result": f"image de {meta['lines']} lignes", "name": job["name"]})
    log.info("Image %s : %d lignes", job["name"], meta["lines"])


if __name__ == "__main__":
    try:
        with open(JOB_FILE, encoding="utf-8") as f:
            job = json.load(f)
    except (OSError, ValueError):
        job = None
    if job and time.time() < job.get("los", 0) and not os.path.exists(JOB_FILE + ".done"):
        try:
            receive(job)
        except Exception as exc:
            log.exception("Réception")
            finish({"result": f"erreur : {exc}"})
    while True:  # au repos jusqu'à la prochaine bascule
        time.sleep(3600)
