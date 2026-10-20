#!/bin/bash
# Installation de la bascule de clé SDR sur F4JCF-SDR-STATION.
# Usage : curl -fsSL https://raw.githubusercontent.com/F4JCF/STATION-SDR/main/raspberry/install_sdr_switch.sh | sudo bash
set -e
BASE="https://raw.githubusercontent.com/F4JCF/STATION-SDR/main/raspberry"

[ "$(id -u)" -eq 0 ] || { echo "Lancer avec sudo."; exit 1; }
command -v rtl_fm >/dev/null || apt-get install -y rtl-sdr

echo "== Téléchargement"
curl -fsSL -o /usr/local/bin/sdr_switch.py "$BASE/sdr_switch.py"
curl -fsSL -o /etc/systemd/system/sdr-switch.service "$BASE/sdr-switch.service"
mkdir -p /etc/systemd/system/direwolf.service.d
curl -fsSL -o /etc/systemd/system/direwolf.service.d/sdr.conf "$BASE/direwolf-sdr.conf"

echo "== Direwolf : entrée audio depuis rtl_fm"
if [ -f /etc/direwolf.conf ] && ! grep -q "^ADEVICE stdin" /etc/direwolf.conf; then
    cp -n /etc/direwolf.conf /etc/direwolf.conf.avant-sdr
    sed -i -E 's/^ADEVICE.*/ADEVICE stdin null/' /etc/direwolf.conf
fi

echo "== Les modules ne démarrent plus seuls : c'est la bascule qui choisit"
for u in readsb dump1090-fa dump1090 ais-catcher aiscatcher AIS-catcher rtl-ais rtl_ais direwolf openwebrx; do
    if systemctl list-unit-files "$u.service" --no-legend 2>/dev/null | grep -q "$u.service"; then
        systemctl disable "$u.service" >/dev/null 2>&1 || true
        echo "   - $u"
    fi
done

systemctl daemon-reload
systemctl enable sdr-switch.service >/dev/null
systemctl restart sdr-switch.service
sleep 6
echo "== État"
curl -s http://localhost:5006/api/sdr/mode; echo
