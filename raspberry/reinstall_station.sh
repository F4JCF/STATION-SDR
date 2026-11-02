#!/bin/bash
# Réinstallation complète de F4JCF-SDR-STATION sur une carte SD neuve.
#
# Préparer la carte avec Raspberry Pi Imager : « Raspberry Pi OS Lite »,
# nom d'hôte F4JCF-SDR-STATION, utilisateur f4jcf, SSH activé, puis :
#
#   scp station-sdr-config-AAAAMMJJ-HHMM.tar.gz f4jcf@F4JCF-SDR-STATION:
#   ssh f4jcf@F4JCF-SDR-STATION
#   curl -fsSL https://raw.githubusercontent.com/F4JCF/STATION-SDR/vX.Y.Z/raspberry/reinstall_station.sh \
#        | sudo bash -s vX.Y.Z ~/station-sdr-config-AAAAMMJJ-HHMM.tar.gz
#
# Durée : 1 à 2 heures sur un Pi 3 (compilations). Chaque étape est
# indépendante : si l'une échoue, les suivantes continuent et un bilan
# s'affiche à la fin. Relancer le script ne casse rien.
set -u
[ "$(id -u)" -eq 0 ] || { echo "Lancer avec sudo."; exit 1; }
REF="${1:-main}"
BACKUP="${2:-}"
BASE="https://raw.githubusercontent.com/F4JCF/STATION-SDR/$REF/raspberry"
USER_NAME="${SUDO_USER:-f4jcf}"
OK=() KO=()

step() {  # step "titre" commande...
    local title="$1"; shift
    echo; echo "=================== $title"
    if "$@" </dev/null; then OK+=("$title"); else KO+=("$title"); echo "!!! Échec : $title (on continue)"; fi
}
run_remote() {  # run_remote script.sh [args]
    local s="$1"; shift
    curl -fsSL "$BASE/$s" -o "/tmp/$s" && bash "/tmp/$s" "$@"
}

base_packages() {
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
        curl git python3 python3-flask python3-requests python3-numpy rtl-sdr librtlsdr-dev \
        direwolf sox build-essential
}
dvb_blacklist() {
    printf 'blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830\nblacklist rtl2832_sdr\n' \
        > /etc/modprobe.d/blacklist-rtlsdr.conf
}
adsb() {
    bash -c "$(curl -fsSL https://github.com/wiedehopf/adsb-scripts/raw/master/readsb-install.sh)" &&
    bash -c "$(curl -fsSL https://github.com/wiedehopf/tar1090/raw/master/install.sh)"
}
aprs_api() {
    curl -fsSL "$BASE/aprs_api.py" -o "/home/$USER_NAME/aprs_api.py" &&
    chown "$USER_NAME:$USER_NAME" "/home/$USER_NAME/aprs_api.py" &&
    curl -fsSL "$BASE/aprs-api.service" -o /etc/systemd/system/aprs-api.service &&
    systemctl daemon-reload && systemctl enable --now aprs-api.service
}
openwebrx() {
    command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
    docker inspect openwebrx >/dev/null 2>&1 || docker create --name openwebrx --restart=no \
        --device /dev/bus/usb -p 8073:8073 -v openwebrx-settings:/var/lib/openwebrx \
        -v openwebrx-etc:/etc/openwebrx jketterl/openwebrx:stable
}
restore_backup() {
    [ -n "$BACKUP" ] || { echo "Pas de sauvegarde indiquée : configuration par défaut."; return 0; }
    [ -f "$BACKUP" ] || { echo "Sauvegarde introuvable : $BACKUP"; return 1; }
    tar -xzf "$BACKUP" -C / --exclude LISEZMOI.txt --exclude INVENTAIRE.txt &&
    tar -xzf "$BACKUP" -C /root INVENTAIRE.txt 2>/dev/null; true
}

step "Paquets de base" base_packages
step "Clé RTL-SDR : pilote TV désactivé" dvb_blacklist
step "ADS-B (readsb + tar1090)" adsb
step "Service de bascule de la clé" run_remote install_sdr_switch.sh
step "Surveillance et notifications" run_remote install_station_monitor.sh "$REF"
step "API APRS (Direwolf)" aprs_api
step "AIS (AIS-catcher)" run_remote install_ais.sh
step "Capteurs 433, APRS ISS, veille 121,5" run_remote install_extras.sh "$REF"
step "Balises 406 MHz (compilation)" run_remote install_beacon406.sh "$REF"
step "POCSAG" run_remote install_pocsag.sh "$REF"
step "OpenWebRX (Docker)" openwebrx
step "Restauration de la sauvegarde" restore_backup
step "Protection de la carte SD" run_remote protect_sd.sh "$REF"
systemctl daemon-reload
systemctl restart sdr-switch.service station-monitor.service 2>/dev/null

echo
echo "=================== Bilan"
for s in "${OK[@]}"; do echo "  OK     $s"; done
for s in "${KO[@]}"; do echo "  ÉCHEC  $s"; done
cat <<EOF

Restent à faire à la main si vous les utilisez : SvxLink (install_svxlink_fr.sh),
Winlink/Pat (install_winlink.sh), écran Nextion, iGate LoRa (indépendant du Pi).
L'inventaire de l'ancien Pi est dans /root/INVENTAIRE.txt (si la sauvegarde
l'incluait). Redémarrez ensuite le Pi : sudo reboot
EOF
