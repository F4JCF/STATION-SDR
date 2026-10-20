#!/bin/bash
# Installation d'AIS-catcher sur F4JCF-SDR-STATION, intégré à la bascule de clé SDR.
# Usage : curl -fsSL https://raw.githubusercontent.com/F4JCF/STATION-SDR/main/raspberry/install_ais.sh | sudo bash
set -e
[ "$(id -u)" -eq 0 ] || { echo "Lancer avec sudo."; exit 1; }

# Position de la station (la même que readsb).
LAT="43.590541"
LON="7.002072"

echo "== Installation d'AIS-catcher (installateur officiel, paquet précompilé)"
INST=/tmp/aiscatcher-install
curl -fsSL -o "$INST" https://raw.githubusercontent.com/jvde-github/AIS-catcher/main/scripts/aiscatcher-install
if ! bash "$INST" -p; then
    echo "   Paquet indisponible pour ce Pi : compilation depuis les sources (15 à 30 min)..."
    bash "$INST"
fi
command -v AIS-catcher >/dev/null || { echo "AIS-catcher introuvable après installation."; exit 1; }

echo "== Configuration : serveur web sur le port 8100 (lu par l'appli STATION-SDR)"
python3 - "$LAT" "$LON" <<'PY'
import json, os, sys
path = "/etc/AIS-catcher/config.json"
try:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
except (OSError, ValueError):
    cfg = {"config": "aiscatcher", "version": 1}
server = cfg.get("server") or {}
server.update({"active": True, "port": "8100", "lat": sys.argv[1], "lon": sys.argv[2], "share_loc": True})
for key in ("cdn", "plugin_dir"):
    if key in server and not os.path.isdir(str(server[key])):
        del server[key]
cfg["server"] = server
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
print("   config.json mis à jour")
PY

echo "== AIS-catcher ne démarre plus seul : c'est la bascule de clé qui le lance"
systemctl stop ais-catcher.service 2>/dev/null || true
systemctl disable ais-catcher.service >/dev/null 2>&1 || true

if systemctl list-unit-files sdr-switch.service --no-legend 2>/dev/null | grep -q sdr-switch; then
    systemctl restart sdr-switch.service
    sleep 3
    echo "== Bascule de clé :"
    curl -s http://localhost:5006/api/sdr/mode; echo
fi
echo "Terminé. Ouvrez la page AIS dans STATION-SDR pour lui donner la clé."
