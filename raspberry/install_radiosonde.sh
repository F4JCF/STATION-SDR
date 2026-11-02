#!/bin/bash
# Module « Radiosondes » de F4JCF-SDR-STATION : radiosonde_auto_rx (Project Horus)
# en conteneur Docker. Il cherche les radiosondes météo (400–406 MHz), décode
# leur position et envoie les données à SondeHub. Démarré par la bascule de clé.
# Usage : ... | sudo bash -s <version>
set -e
[ "$(id -u)" -eq 0 ] || { echo "Lancer avec sudo."; exit 1; }
REF="${1:-main}"
BASE="https://raw.githubusercontent.com/F4JCF/STATION-SDR/$REF/raspberry"
IMAGE="ghcr.io/projecthorus/radiosonde_auto_rx:latest"
DIR=/opt/radiosonde
CALL="F4JCF"; LAT="43.590541"; LON="7.002072"; ALT="200"

echo "== Docker"
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
docker --version

echo "== Configuration ($DIR/station.cfg)"
mkdir -p "$DIR/log"
if [ ! -f "$DIR/station.cfg" ]; then
    curl -fsSL -o "$DIR/station.cfg" \
        https://raw.githubusercontent.com/projecthorus/radiosonde_auto_rx/master/auto_rx/station.cfg.example
fi
python3 - "$DIR/station.cfg" "$CALL" "$LAT" "$LON" "$ALT" <<'PY'
import re, sys
path, call, lat, lon, alt = sys.argv[1:]
txt = open(path, encoding="utf-8").read()
for key, val in (("uploader_callsign", call), ("station_lat", lat), ("station_lon", lon), ("station_alt", alt),
                 ("max_freq", "406.0"), ("min_freq", "400.05"), ("web_port", "5000"),
                 ("uploader_antenna", "Discone")):
    txt, n = re.subn(rf"(?m)^{key}\s*=.*$", f"{key} = {val}", txt, count=1)
    print(f"   {key} = {val}" + ("" if n else "  (clé absente !)"))
open(path, "w", encoding="utf-8").write(txt)
PY

echo "== Image $IMAGE (téléchargement, quelques minutes)"
docker pull "$IMAGE"
docker rm -f radiosonde_auto_rx >/dev/null 2>&1 || true
docker create --name radiosonde_auto_rx --restart=no --network host --device /dev/bus/usb \
    -v "$DIR/station.cfg:/opt/auto_rx/station.cfg:ro" -v "$DIR/log:/opt/auto_rx/log" "$IMAGE" >/dev/null
echo "   conteneur radiosonde_auto_rx créé (démarré par la bascule de clé)"

echo "== Bascule de clé et surveillance"
curl -fsSL -o /usr/local/bin/sdr_switch.py "$BASE/sdr_switch.py"
curl -fsSL -o /usr/local/bin/station_monitor.py "$BASE/station_monitor.py"
systemctl restart sdr-switch.service
systemctl restart station-monitor.service 2>/dev/null || true
sleep 4
curl -s http://localhost:5006/api/sdr/mode | grep -q '"radiosonde": \["docker:radiosonde_auto_rx"\]' \
  && echo "OK : module Radiosondes disponible." || { echo "Attention : module Radiosondes non vu par la bascule."; exit 1; }
