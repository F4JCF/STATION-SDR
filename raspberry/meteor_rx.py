#!/usr/bin/env python3
"""Réception d'un passage Meteor-M (lancé par la bascule de clé, mode
« meteor ») : rtl_sdr → meteor_demod → meteor_decode, en direct, jusqu'au
coucher du satellite. Le résultat (PNG, vignette, fiche) est rangé dans
/var/lib/meteor/images. Ensuite le service reste au repos jusqu'à la
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


def post_process(job, png):
    from PIL import Image
    img = Image.open(png)
    w, h = img.size
    if h < 64:
        return None
    if job.get("northbound"):
        img = img.rotate(180)
        img.save(png)
    thumb = img.convert("RGB")
    thumb.thumbnail((480, 900))
    thumb.save(png[:-4] + ".jpg", quality=82)
    meta = {"name": job["name"], "sat": job["sat"], "aos": job["aos"], "max_el": job["max_el"],
            "width": w, "lines": h, "size": os.path.getsize(png), "time": time.time()}
    with open(png[:-4] + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f)
    return meta


def receive(job):
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
        finish({"result": "image trop courte (réception incomplète)"})
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
