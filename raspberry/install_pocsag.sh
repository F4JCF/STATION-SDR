#!/bin/bash
# Module POCSAG de F4JCF-SDR-STATION (rtl_fm + multimon-ng).
# Usage : curl -fsSL .../install_pocsag.sh | sudo bash -s <version>
set -e
[ "$(id -u)" -eq 0 ] || { echo "Lancer avec sudo."; exit 1; }
REF="${1:-main}"
BASE="https://raw.githubusercontent.com/F4JCF/STATION-SDR/$REF/raspberry"

echo "== multimon-ng et rtl-sdr (paquets Debian)"
apt-get install -y multimon-ng rtl-sdr >/dev/null
multimon-ng -h 2>&1 | head -1

echo "== Fichiers"
curl -fsSL -o /usr/local/bin/pocsag_api.py "$BASE/pocsag_api.py"
curl -fsSL -o /etc/systemd/system/pocsag.service "$BASE/pocsag.service"
curl -fsSL -o /usr/local/bin/sdr_switch.py "$BASE/sdr_switch.py"
curl -fsSL -o /usr/local/bin/station_monitor.py "$BASE/station_monitor.py"
chmod 755 /usr/local/bin/pocsag_api.py
systemctl daemon-reload
# Comme les autres modules, c'est la bascule de clé qui le démarre.
systemctl disable pocsag.service >/dev/null 2>&1 || true
systemctl restart sdr-switch.service
systemctl restart station-monitor.service 2>/dev/null || true
sleep 4
curl -s http://localhost:5006/api/sdr/mode | grep -q '"pocsag": \["pocsag"\]' \
  && echo "OK : module POCSAG disponible." || echo "Attention : module POCSAG non vu par la bascule."
