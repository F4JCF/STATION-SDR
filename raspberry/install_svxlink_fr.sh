#!/bin/bash
# Voix française (Agnès, pack F8ASB) pour SvxLink sur F4JCF-SDR-STATION.
set -e
[ "$(id -u)" -eq 0 ] || { echo "Lancer avec sudo."; exit 1; }
PACK_REF="91ec499c0289149ed0b459c110bc5d276594a905"
SOUNDS=/usr/share/svxlink/sounds
CONF=/etc/svxlink/svxlink.conf

echo "== Téléchargement du pack de voix (environ 50 Mo)"
TMP=$(mktemp -d)
curl -fsSL "https://codeload.github.com/F8ASB/fr_FR_Agnes/tar.gz/$PACK_REF" | tar -xz -C "$TMP"
SRC=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)
[ -f "$SRC/Default/phonetic_f.wav" ] || { echo "Pack incomplet, abandon."; exit 1; }

echo "== Installation dans $SOUNDS/fr_FR"
mkdir -p "$SOUNDS"
rm -rf "$SOUNDS/fr_FR"
mv "$SRC" "$SOUNDS/fr_FR"
rm -rf "$TMP"
chmod -R a+rX "$SOUNDS/fr_FR"

echo "== Langue de SvxLink : fr_FR (sauvegarde : $CONF.avant-fr)"
cp -n "$CONF" "$CONF.avant-fr"
python3 - "$CONF" <<'PY'
import re, sys
path = sys.argv[1]
lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
logics = []
for l in lines:
    m = re.match(r"\s*LOGICS\s*=\s*(.*)", l)
    if m:
        logics = [x.strip() for x in m.group(1).split(",") if x.strip()]
        break
out, section, done = [], None, set()
for l in lines:
    m = re.match(r"\s*\[([^\]]+)\]", l)
    if m:
        if section in logics and section not in done:
            out.append("DEFAULT_LANG=fr_FR"); done.add(section)
        section = m.group(1)
    if section in logics and re.match(r"\s*#?\s*DEFAULT_LANG\s*=", l):
        if section not in done:
            out.append("DEFAULT_LANG=fr_FR"); done.add(section)
        continue
    out.append(l)
if section in logics and section not in done:
    out.append("DEFAULT_LANG=fr_FR"); done.add(section)
open(path, "w", encoding="utf-8").write("\n".join(out))
print("   logiques passées en français :", ", ".join(sorted(done)) or "aucune")
PY

echo "== Redémarrage de SvxLink"
systemctl restart svxlink
sleep 8
if grep -q "Could not find audio clip" <(tail -n 200 /var/log/svxlink | sed -n '/Initialization done/,$p'); then
    echo "Attention : des annonces manquent encore :"
    tail -n 200 /var/log/svxlink | sed -n '/Initialization done/,$p' | grep "Could not find audio clip" | head -5
else
    echo "OK : SvxLink a redémarré en français."
fi

echo
echo "== Pour le réglage audio : cartes son et réglages de capture"
arecord -l 2>/dev/null | grep -E "^carte|^card" || true
for c in $(arecord -l 2>/dev/null | sed -nE 's/^(carte|card) ([0-9]+):.*/\2/p' | sort -u); do
    echo "-- carte $c"
    amixer -c "$c" scontents 2>/dev/null | grep -E "Simple mixer control|Capture|Mic|Limits" | head -12
done
