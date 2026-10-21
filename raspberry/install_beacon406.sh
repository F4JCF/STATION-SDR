#!/bin/bash
# Installation du décodeur de balises de détresse 406 MHz sur F4JCF-SDR-STATION.
# Décodeur : dec406 de Fabrice Morel (licence MIT), compilé pour RTL-SDR seul.
# Usage : curl -fsSL .../install_beacon406.sh | sudo bash -s <version>
set -e
[ "$(id -u)" -eq 0 ] || { echo "Lancer avec sudo."; exit 1; }
REF="${1:-main}"
BASE="https://raw.githubusercontent.com/F4JCF/STATION-SDR/$REF/raspberry"
DEC406_COMMIT="6859428"
SRC=/opt/dec406-src

echo "== Paquets nécessaires (compilation)"
apt-get install -y build-essential git librtlsdr-dev libfftw3-dev >/dev/null

echo "== Récupération du décodeur dec406 (version $DEC406_COMMIT)"
rm -rf "$SRC"
git clone -q https://github.com/moricef/Decode_sarsat_406_v1g_v2g "$SRC"
git -C "$SRC" checkout -q "$DEC406_COMMIT"

echo "== Compilation (quelques minutes sur un Pi 3)"
cd "$SRC"
CF="-O2 -Iinclude"
gcc $CF -DHAVE_RTLSDR -o dec406_scan src/main_scan_unified.c src/backend_rtlsdr.c src/scanner.c \
    src/dec406.c src/dec406_v1g.c src/dec406_v2g.c src/display_utils.c src/fgb_iq_demod.c src/scan_alert.c \
    src/dsss_demod.c src/rrc_filter.c src/symbol_sync.c src/costas4.c src/despread.c src/freq_acq.c \
    -lm -lpthread -lrtlsdr -fopenmp -lgomp -lfftw3f
gcc $CF -o dec406_hex src/dec406_hex.c src/dec406.c src/dec406_v1g.c src/dec406_v2g.c src/display_utils.c -lm
mkdir -p /opt/dec406
install -m 755 dec406_scan dec406_hex /opt/dec406/
cp LICENSE /opt/dec406/LICENSE
/opt/dec406/dec406_hex FFFED090FD15E0059FEFFC28BEB861F0FABE 2>/dev/null | grep -q "BEACON DECODE" \
    && echo "   décodeur OK" || { echo "Le décodeur ne fonctionne pas."; exit 1; }

echo "== Service beacon406 (port 5009)"
curl -fsSL -o /usr/local/bin/beacon406_api.py "$BASE/beacon406_api.py"
curl -fsSL -o /etc/systemd/system/beacon406.service "$BASE/beacon406.service"
curl -fsSL -o /usr/local/bin/sdr_switch.py "$BASE/sdr_switch.py"
curl -fsSL -o /usr/local/bin/station_monitor.py "$BASE/station_monitor.py"
systemctl daemon-reload
# Comme les autres modules : il ne démarre pas tout seul, c'est la bascule
# de clé SDR qui le lance quand on ouvre la page « Balises de détresse ».
systemctl disable beacon406.service >/dev/null 2>&1 || true
systemctl restart sdr-switch.service
systemctl restart station-monitor.service 2>/dev/null || true
sleep 4
curl -s http://localhost:5006/api/sdr/mode | grep -q '"beacon": \["beacon406"\]' \
    && echo "OK : module « Balise 406 » disponible dans la bascule de clé." \
    || echo "Attention : la bascule ne voit pas encore le module (redémarrer sdr-switch)."
