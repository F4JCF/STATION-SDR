#!/bin/bash
# Module « Images satellites Meteor-M » de F4JCF-SDR-STATION.
# Décodeurs libres meteor_demod et meteor_decode (Davide Belloli, dbdexter-dev),
# compilés sur le Pi ; planification des passages (SGP4) et service de réception.
# Usage : ... | sudo bash -s <version>
set -e
[ "$(id -u)" -eq 0 ] || { echo "Lancer avec sudo."; exit 1; }
REF="${1:-main}"
BASE="https://raw.githubusercontent.com/F4JCF/STATION-SDR/$REF/raspberry"
SRC=/opt/meteor-src

echo "== Paquets (compilation, SGP4, images)"
apt-get install -y build-essential cmake git libpng-dev rtl-sdr python3-sgp4 python3-pil >/dev/null

echo "== Compilation de meteor_demod et meteor_decode (quelques minutes)"
rm -rf "$SRC"; mkdir -p "$SRC"
for repo in meteor_demod meteor_decode; do
    git clone -q --depth 1 "https://github.com/dbdexter-dev/$repo" "$SRC/$repo"
    mkdir -p "$SRC/$repo/build"
    ( cd "$SRC/$repo/build" && cmake -DENABLE_TUI=OFF -DCMAKE_BUILD_TYPE=Release .. >/dev/null && make -j2 >/dev/null && make install >/dev/null )
    echo "   $repo installé"
done
command -v meteor_demod >/dev/null && command -v meteor_decode >/dev/null || { echo "Décodeurs introuvables."; exit 1; }

echo "== Services"
curl -fsSL -o /usr/local/bin/meteor_api.py "$BASE/meteor_api.py"
curl -fsSL -o /usr/local/bin/meteor_rx.py "$BASE/meteor_rx.py"
chmod 755 /usr/local/bin/meteor_api.py /usr/local/bin/meteor_rx.py
curl -fsSL -o /etc/systemd/system/meteor-api.service "$BASE/meteor-api.service"
curl -fsSL -o /etc/systemd/system/meteor-rx.service "$BASE/meteor-rx.service"
curl -fsSL -o /usr/local/bin/sdr_switch.py "$BASE/sdr_switch.py"
curl -fsSL -o /usr/local/bin/station_monitor.py "$BASE/station_monitor.py"
mkdir -p /var/lib/meteor/images /run/meteor
systemctl daemon-reload
systemctl disable meteor-rx.service >/dev/null 2>&1 || true   # démarré par la bascule de clé
systemctl enable meteor-api.service >/dev/null
systemctl restart meteor-api.service
systemctl restart sdr-switch.service
systemctl restart station-monitor.service 2>/dev/null || true
sleep 6
curl -s http://localhost:5006/api/sdr/mode | grep -q '"meteor": \["meteor-rx"\]' \
  && echo "OK : module Meteor disponible." || { echo "Attention : module Meteor non vu par la bascule."; exit 1; }
curl -s http://localhost:5013/api/meteor/status | python3 -c "import json,sys;d=json.load(sys.stdin);print('   prochains passages :', len(d.get('passes') or []), '·', d.get('error') or 'OK')" || true
