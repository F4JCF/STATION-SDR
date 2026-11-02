#!/bin/bash
# Protection de la carte SD de F4JCF-SDR-STATION et chien de garde matériel.
#  - journaux système en mémoire (plus d'écritures permanentes sur la carte) ;
#  - plus de fichier d'échange sur la carte (dphys-swapfile) ;
#  - montage de la racine en noatime et écritures regroupées (commit=120) ;
#  - /tmp en mémoire ;
#  - chien de garde matériel : le Pi redémarre seul s'il se bloque.
# Réversible : voir les fichiers *.avant-protection créés.
# Usage : ... | sudo bash -s <version>
set -e
[ "$(id -u)" -eq 0 ] || { echo "Lancer avec sudo."; exit 1; }

echo "== Journaux système en mémoire (40 Mo maximum)"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/station-sdr.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=40M
EOF
systemctl restart systemd-journald
rm -rf /var/log/journal 2>/dev/null || true

echo "== Fichier d'échange sur la carte"
if systemctl list-unit-files dphys-swapfile.service --no-legend 2>/dev/null | grep -q dphys; then
    dphys-swapfile swapoff 2>/dev/null || true
    dphys-swapfile uninstall 2>/dev/null || true
    systemctl disable --now dphys-swapfile >/dev/null 2>&1 || true
    echo "   désactivé"
else
    echo "   aucun (ou échange en mémoire zram : conservé)"
fi

echo "== Montage de la racine : noatime, écritures regroupées"
cp -n /etc/fstab /etc/fstab.avant-protection
python3 - <<'PY'
import re
path = "/etc/fstab"
lines = open(path, encoding="utf-8").read().splitlines()
out = []
for line in lines:
    parts = line.split()
    if len(parts) >= 4 and not line.lstrip().startswith("#") and parts[1] == "/" and parts[2] == "ext4":
        opts = [o for o in parts[3].split(",") if o and not o.startswith("commit=") and o not in ("atime", "relatime")]
        for o in ("noatime", "commit=120"):
            if o not in opts:
                opts.append(o)
        parts[3] = ",".join(opts)
        line = "\t".join(parts)
    out.append(line)
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
mount -o remount / 2>/dev/null || echo "   (pris en compte au prochain redémarrage)"
grep -E '\s/\s' /etc/fstab | sed 's/^/   /'

echo "== /tmp en mémoire"
if ! findmnt -n /tmp | grep -q tmpfs; then
    if [ -f /usr/share/systemd/tmp.mount ]; then
        cp /usr/share/systemd/tmp.mount /etc/systemd/system/tmp.mount
        systemctl enable tmp.mount >/dev/null 2>&1 || true
        echo "   activé au prochain redémarrage"
    fi
else
    echo "   déjà en mémoire"
fi

echo "== Chien de garde matériel (redémarrage auto si le Pi se fige)"
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/station-sdr-watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=15
RebootWatchdogSec=2min
EOF
systemctl daemon-reexec

echo "== Vérification du système de fichiers à chaque démarrage"
if [ -f /boot/firmware/cmdline.txt ]; then CMD=/boot/firmware/cmdline.txt; else CMD=/boot/cmdline.txt; fi
if [ -f "$CMD" ] && ! grep -q "fsck.repair=yes" "$CMD"; then
    cp -n "$CMD" "$CMD.avant-protection"
    sed -i 's/$/ fsck.repair=yes/' "$CMD"
fi

mkdir -p /var/lib/sdr-switch
echo "OK : protection de la carte SD en place. Un redémarrage du Pi est conseillé."
