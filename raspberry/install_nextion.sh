#!/bin/bash
# Écran Nextion de F4JCF-SDR-STATION : résumé de la station (nextion-station).
# Remplace l'ancien pilote d'écran (nextion-bridge / aprs-nextion), qui est
# arrêté et désactivé (réversible : systemctl enable --now nextion-bridge).
# Usage : curl -fsSL .../install_nextion.sh | sudo bash -s <version>
set -e
[ "$(id -u)" -eq 0 ] || { echo "Lancer avec sudo."; exit 1; }
REF="${1:-main}"
BASE="https://raw.githubusercontent.com/F4JCF/STATION-SDR/$REF/raspberry"

echo "== pyserial"
apt-get install -y python3-serial >/dev/null
python3 -c "import serial" || { echo "pyserial introuvable."; exit 1; }

echo "== Port série de l'écran"
PORT=/dev/ttyAMA0
[ -e "$PORT" ] || PORT=/dev/serial0
[ -e "$PORT" ] || { echo "Aucun port série (ttyAMA0 / serial0) : écran non raccordé ?"; exit 1; }
echo "   $PORT"

echo "== Ancien pilote d'écran"
for u in nextion-bridge aprs-nextion; do
  if systemctl list-unit-files "$u.service" >/dev/null 2>&1 && systemctl list-unit-files | grep -q "^$u.service"; then
    systemctl disable --now "$u.service" >/dev/null 2>&1 || true
    echo "   $u arrêté et désactivé"
  fi
done

echo "== Service"
curl -fsSL -o /usr/local/bin/nextion_station.py "$BASE/nextion_station.py"
curl -fsSL -o /etc/systemd/system/nextion-station.service "$BASE/nextion-station.service"
chmod 755 /usr/local/bin/nextion_station.py
[ -f /etc/nextion-station.json ] || echo "{\"port\": \"$PORT\", \"baud\": 9600, \"font\": 0, \"font_big\": 0, \"row_h\": 34, \"cycle_s\": 20, \"dim\": 80}" > /etc/nextion-station.json
systemctl daemon-reload
systemctl enable nextion-station.service >/dev/null
systemctl restart nextion-station.service
sleep 4
systemctl is-active --quiet nextion-station.service && echo "OK : l'écran Nextion affiche la station." \
  || { echo "Attention : service nextion-station arrêté."; journalctl -u nextion-station -n 15 --no-pager; exit 1; }
