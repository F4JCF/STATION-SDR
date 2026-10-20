#!/bin/bash
# Installation de l'API d'activité SvxLink (port 5007) sur F4JCF-SDR-STATION.
set -e
[ "$(id -u)" -eq 0 ] || { echo "Lancer avec sudo."; exit 1; }
REF="${1:-main}"
BASE="https://raw.githubusercontent.com/F4JCF/STATION-SDR/$REF/raspberry"
curl -fsSL -o /usr/local/bin/svxlink_api.py "$BASE/svxlink_api.py"
curl -fsSL -o /etc/systemd/system/svxlink-api.service "$BASE/svxlink-api.service"
systemctl daemon-reload
systemctl enable --now svxlink-api.service >/dev/null
systemctl restart svxlink-api.service
for i in $(seq 1 10); do
    out=$(curl -s http://localhost:5007/api/svxlink/status) && [ -n "$out" ] && { echo "$out" | cut -c1-400; echo; echo "OK"; exit 0; }
    sleep 1
done
journalctl -u svxlink-api -n 20 --no-pager
