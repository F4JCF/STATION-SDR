#!/bin/bash
# Installation de Pat (client Winlink) sur F4JCF-SDR-STATION.
# Pat tourne en service pour l'utilisateur f4jcf, avec son interface web
# et son API sur le port 8080 (lue par l'appli STATION-SDR).
set -e
[ "$(id -u)" -eq 0 ] || { echo "Lancer avec sudo."; exit 1; }
PAT_VERSION="1.0.0"
PI_USER="f4jcf"
MYCALL="F4JCF"
LOCATOR="JN33LP"

ARCH=$(dpkg --print-architecture)
case "$ARCH" in
    armhf|arm64|amd64) ;;
    *) echo "Architecture $ARCH non prise en charge."; exit 1 ;;
esac

if ! command -v pat >/dev/null; then
    echo "== Téléchargement de Pat $PAT_VERSION ($ARCH)"
    DEB="/tmp/pat_${PAT_VERSION}_linux_${ARCH}.deb"
    curl -fsSL -o "$DEB" "https://github.com/la5nta/pat/releases/download/v${PAT_VERSION}/pat_${PAT_VERSION}_linux_${ARCH}.deb"
    apt-get install -y "$DEB"
fi
pat version 2>/dev/null || true

CFG_DIR="/home/$PI_USER/.config/pat"
CFG="$CFG_DIR/config.json"
if [ ! -f "$CFG" ]; then
    echo "== Création de la configuration par défaut"
    sudo -u "$PI_USER" timeout 8 pat http >/dev/null 2>&1 || true
fi
[ -f "$CFG" ] || { echo "Configuration Pat introuvable ($CFG)."; exit 1; }

echo "== Réglages : indicatif $MYCALL, locator $LOCATOR, interface web sur le réseau local (port 8080)"
cp -n "$CFG" "$CFG.avant-station-sdr"
python3 - "$CFG" "$MYCALL" "$LOCATOR" <<'PY'
import json, sys
path, mycall, locator = sys.argv[1:4]
with open(path, encoding="utf-8") as f:
    cfg = json.load(f)
if not cfg.get("mycall"):
    cfg["mycall"] = mycall
if not cfg.get("locator"):
    cfg["locator"] = locator
cfg["http_addr"] = "0.0.0.0:8080"
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
PY
chown -R "$PI_USER:$PI_USER" "$CFG_DIR"

echo "== Démarrage du service pat@$PI_USER"
systemctl daemon-reload
systemctl enable "pat@$PI_USER.service" >/dev/null 2>&1
systemctl restart "pat@$PI_USER.service"
for i in $(seq 1 10); do
    out=$(curl -s http://localhost:8080/api/status) && [ -n "$out" ] && { echo "$out"; echo "OK : Pat répond sur le port 8080."; break; }
    sleep 1
done
if ! grep -q '"secure_login_password": "[^"]' "$CFG"; then
    echo
    echo "IMPORTANT : le mot de passe de votre compte Winlink n'est pas encore renseigné."
    echo "Ouvrez http://$(hostname -I | awk '{print $1}'):8080 dans un navigateur, menu Settings,"
    echo "et saisissez-le vous-même (il n'est jamais transmis à l'appli STATION-SDR)."
fi
