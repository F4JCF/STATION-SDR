#!/bin/bash
# Modules supplémentaires de F4JCF-SDR-STATION : capteurs 433 MHz (rtl_433)
# et APRS via l'ISS (145,825 MHz). Usage : ... | sudo bash -s <version>
set -e
[ "$(id -u)" -eq 0 ] || { echo "Lancer avec sudo."; exit 1; }
REF="${1:-main}"
BASE="https://raw.githubusercontent.com/F4JCF/STATION-SDR/$REF/raspberry"

echo "== rtl_433 (paquet Debian)"
apt-get install -y rtl-433 >/dev/null
command -v rtl_433 >/dev/null && rtl_433 -V 2>&1 | head -1 || { echo "rtl_433 introuvable."; exit 1; }

echo "== Fichiers"
curl -fsSL -o /usr/local/bin/rtl433_api.py "$BASE/rtl433_api.py"
curl -fsSL -o /etc/systemd/system/rtl433.service "$BASE/rtl433.service"
curl -fsSL -o /etc/systemd/system/direwolf-iss.service "$BASE/direwolf-iss.service"
curl -fsSL -o /usr/local/bin/sdr_switch.py "$BASE/sdr_switch.py"
curl -fsSL -o /usr/local/bin/station_monitor.py "$BASE/station_monitor.py"
systemctl daemon-reload
# Comme les autres modules, c'est la bascule de clé qui les démarre.
systemctl disable rtl433.service direwolf-iss.service >/dev/null 2>&1 || true
systemctl restart sdr-switch.service
systemctl restart station-monitor.service 2>/dev/null || true
sleep 4
out=$(curl -s http://localhost:5006/api/sdr/mode)
echo "$out" | grep -q '"433": \["rtl433"\]' && echo "OK : module 433 MHz disponible." || echo "Attention : module 433 non vu par la bascule."
echo "$out" | grep -q '"iss": \["direwolf-iss"\]' && echo "OK : module ISS disponible." || echo "Attention : module ISS non vu par la bascule."
