#!/bin/bash
# Installe le service de surveillance (statistiques + notifications, port 5008)
# et met à jour le service de bascule (maintenance, sauvegarde).
# Usage : curl -fsSL .../install_station_monitor.sh | sudo bash -s <commit>
set -e
[ "$(id -u)" -eq 0 ] || { echo "Lancer avec sudo."; exit 1; }
REF="${1:-main}"
BASE="https://raw.githubusercontent.com/F4JCF/STATION-SDR/$REF/raspberry"
curl -fsSL -o /usr/local/bin/station_monitor.py "$BASE/station_monitor.py"
curl -fsSL -o /etc/systemd/system/station-monitor.service "$BASE/station-monitor.service"
curl -fsSL -o /usr/local/bin/sdr_switch.py "$BASE/sdr_switch.py"
systemctl daemon-reload
systemctl enable station-monitor.service >/dev/null 2>&1
systemctl restart sdr-switch.service station-monitor.service
for i in $(seq 1 10); do
    if curl -s http://localhost:5008/api/monitor/config >/dev/null && curl -s http://localhost:5006/api/system/log-units >/dev/null; then
        echo "OK : surveillance (5008) et bascule (5006) à jour."; exit 0
    fi
    sleep 1
done
journalctl -u station-monitor -n 20 --no-pager
