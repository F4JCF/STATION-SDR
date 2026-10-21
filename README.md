# STATION-SDR

Tableau de bord Windows pour le Raspberry Pi multi-fonctions **F4JCF-SDR-STATION**
(ADS-B, météo locale, services système, récepteurs SDR).

## Fonctionnalités

- **Météo** — relevé local (`meteo_local.py`, port 5001), carte avec calque
  radar pluie en direct au choix, via l'API publique et gratuite
  [RainViewer](https://www.rainviewer.com/), sans clé requise — nécessite
  une connexion Internet sur le PC ; le zoom de la carte est
  automatiquement réduit pendant l'affichage du radar (RainViewer ne
  documente ses tuiles que jusqu'au zoom 7)
- **Trafic aérien (ADS-B)** — liste des avions détectés (`readsb`/`tar1090`, port 8504),
  carte interactive avec icônes orientées/colorées par altitude, traînées de
  vol, recherche par indicatif/hex, et fiche détaillée au clic façon
  tar1090 : vitesse sol (kt/km-h), altitude baro./géo., vitesse verticale,
  cap, position, distance à la station, source (ADS-B/MLAT/TIS-B), RSSI,
  messages, ancienneté, squawk, plus compagnie, type d'appareil,
  immatriculation et photo via les services publics
  [hexdb.io](https://hexdb.io) et [planespotters.net](https://www.planespotters.net) —
  nécessite une connexion Internet sur le PC qui fait tourner l'application
- **Services** — état des services système : AIS, Direwolf, météo locale, OpenWebRX,
  readsb, gestionnaire SDR, SvxLink (`station_api.py`, port 5003)
- **Récepteurs SDR** — quel récepteur est utilisé par quelle fonction
  (`sdr_manager.py`, port 5002)
- **APRS** — stations entendues (position, distance/direction, commentaire)
  décodées depuis le flux KISS de Direwolf (`aprs_api.py`, port 5004 — voir
  ci-dessous pour le déployer sur le Pi) + état du service avec redémarrage
- **AIS** et **SvxLink** — pages informatives avec statut (en cours/arrêté,
  remonté automatiquement depuis `station_api.py` si le service est présent
  dans sa réponse) ; liste détaillée (navires AIS, activité SvxLink) non
  disponible pour l'instant faute d'API dédiée côté Pi
- **Winlink** — module informatif : non installé sur le Raspberry pour
  l'instant
- **OpenWebRX** — vignette qui ouvre directement l'interface web OpenWebRX
  du Pi dans le navigateur par défaut (port configurable dans Paramètres)
- Écran d'accueil en vignettes avec pictogramme par module, et menu
  **Modules** (barre de menus) listant tous les modules pour y accéder
  directement sans repasser par l'accueil
- **Mise à jour automatique** — au démarrage, l'application vérifie s'il existe
  une version plus récente sur GitHub (Releases), propose de l'installer et,
  une fois le téléchargement terminé, **se ferme puis se relance
  automatiquement** avec la nouvelle version (aucune action manuelle requise)

Écran d'accueil en vignettes cliquables, une par module. Rafraîchissement
automatique toutes les 15 secondes (réglable).

## Déployer le module APRS sur le Raspberry Pi

Le dossier `raspberry/` contient le service à installer sur
**F4JCF-SDR-STATION** pour que l'onglet APRS affiche les stations
réellement entendues par Direwolf (sans ça, l'onglet affiche juste l'état
du service Direwolf, sans liste de stations) :

1. Copier les deux fichiers sur le Pi (remplacer `F4JCF-SDR-STATION` par
   son IP si besoin) :
   ```
   scp raspberry/aprs_api.py f4jcf@F4JCF-SDR-STATION:/home/f4jcf/
   scp raspberry/aprs-api.service f4jcf@F4JCF-SDR-STATION:/tmp/
   ```
2. Se connecter en SSH sur le Pi, installer Flask si nécessaire, puis
   installer et démarrer le service :
   ```
   pip3 install flask --break-system-packages   # si pas déjà installé
   sudo mv /tmp/aprs-api.service /etc/systemd/system/aprs-api.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now aprs-api
   sudo systemctl status aprs-api
   ```
3. Vérifier que ça décode bien quelque chose :
   ```
   curl http://localhost:5004/api/aprs/health
   curl http://localhost:5004/api/aprs/stations
   ```

Par défaut, `aprs_api.py` se connecte au port KISS TCP `8001` de Direwolf
en local (`127.0.0.1`). Si votre `direwolf.conf` utilise un autre port
(ligne `KISSPORT`), ajuster la variable d'environnement
`DIREWOLF_KISS_PORT` dans `aprs-api.service` avant de l'installer.

Le décodeur reconnaît les paquets de position APRS au format
non-compressé (le plus courant). Les formats Mic-E et position compressée
ne sont pas décodés pour l'instant : ces stations apparaîtront comme
entendues mais sans position sur la carte.

## Installation

1. Télécharger `STATION-SDR.exe` depuis les
   [Releases](https://github.com/F4JCF/STATION-SDR/releases) du dépôt.
2. Le lancer — aucune installation de Python n'est nécessaire.
3. Dans le menu **STATION-SDR → Paramètres**, vérifier/ajuster :
   - l'adresse IP du Raspberry Pi (par défaut `192.168.1.229`)
   - (optionnel) un jeton GitHub personnel, voir ci-dessous

## Mise à jour automatique

Le dépôt `F4JCF/STATION-SDR` est **public** : la vérification et le
téléchargement des mises à jour fonctionnent sans jeton GitHub. Le champ
**Paramètres → Jeton GitHub** ne reste utile que si le dépôt redevenait
privé un jour.

Au démarrage, l'application compare sa version à la dernière release
GitHub ; si une nouvelle version existe, elle propose de l'installer, puis
se ferme et se relance automatiquement une fois le téléchargement terminé.

### En cas d'échec du redémarrage automatique

Le remplacement du fichier `.exe` se fait via un petit script qui attend la
fermeture de l'application puis déplace le nouveau fichier en place
(plusieurs tentatives si un antivirus retient momentanément le fichier). Si
ça échoue malgré tout :

- un fichier journal détaillé est écrit dans
  `%TEMP%\station_sdr_update.log` ;
- l'application affiche un avertissement au démarrage suivant si le
  remplacement a échoué, avec un lien vers ce journal ;
- en attendant, on peut toujours télécharger et remplacer
  `STATION-SDR.exe` manuellement depuis les
  [Releases](https://github.com/F4JCF/STATION-SDR/releases/latest).

Autre cas possible : la fenêtre s'ouvre puis se referme aussitôt juste
après un redémarrage automatique (nouvelle version qui plante dès le
démarrage, ou une erreur imprévue pendant l'utilisation). Comme
l'exécutable est compilé en mode `--windowed` (sans console), ce genre
d'erreur serait normalement totalement invisible. Pour éviter ça,
l'application :

- enregistre toute erreur de ce type dans
  `%TEMP%\station_sdr_crash.log` ;
- affiche une boîte de dialogue d'erreur au moment du plantage si
  possible, et sinon le signale automatiquement au prochain démarrage
  réussi (une seule fois).

## Publier une nouvelle version (pour le mainteneur)

Entièrement automatique — une seule étape :

1. Mettre à jour `__version__` dans `station_sdr/version.py`, committer et
   pousser sur `main`.

Le commit déclenche `.github/workflows/tag-on-version.yml`, qui crée et
pousse automatiquement le tag `vX.Y.Z` correspondant. Ce tag déclenche à son
tour `.github/workflows/build.yml`, qui compile `STATION-SDR.exe` avec
PyInstaller et publie une Release GitHub avec l'exécutable en pièce jointe.
Aucune action manuelle sur github.com n'est nécessaire.

## Développement local

```
pip install -r requirements.txt
python run.py
```

## Licence

MIT — voir [LICENSE](LICENSE).

## Clé SDR unique : bascule automatique entre modules

Avec une seule clé RTL-SDR, ouvrir un module dans l'appli (Trafic aérien,
AIS, APRS, OpenWebRX) arrête le module précédent et donne la clé au
nouveau. Côté Pi, installation en une commande :

```
curl -fsSL https://raw.githubusercontent.com/F4JCF/STATION-SDR/main/raspberry/install_sdr_switch.sh | sudo bash
```

Le script installe `sdr_switch.py` (port 5006), fait recevoir à Direwolf
le son de `rtl_fm` sur 144,800 MHz (sauvegarde : `/etc/direwolf.conf.avant-sdr`)
et désactive le démarrage automatique des modules : au boot, c'est le
dernier module choisi qui reprend la clé.

## SvxLink : activité en direct

```
curl -fsSL https://raw.githubusercontent.com/F4JCF/STATION-SDR/main/raspberry/install_svxlink_api.sh | sudo bash
```

Installe `svxlink_api.py` (port 5007), qui suit `/var/log/svxlink` : émission,
squelch, module actif, stations EchoLink connectées, journal d'activité et
avertissements (saturation audio, annonces vocales manquantes).

## Winlink (Pat)

```
curl -fsSL https://raw.githubusercontent.com/F4JCF/STATION-SDR/main/raspberry/install_winlink.sh | sudo bash
```

Installe Pat (client Winlink, paquet officiel), configure l'indicatif et le
locator, ouvre son interface web et son API sur le port 8080 (réseau local).
Le mot de passe Winlink se saisit dans Pat (navigateur → Settings), jamais
dans STATION-SDR.

## Surveillance : statistiques, notifications, vigilance

```
curl -fsSL https://raw.githubusercontent.com/F4JCF/STATION-SDR/main/raspberry/install_station_monitor.sh | sudo bash
```

Installe `station_monitor.py` (port 5008) : historique 30 jours (SQLite),
polaires de portée ADS-B / AIS, notifications ntfy / e-mail des événements
de sécurité, vigilance Météo-France (clé API gratuite à saisir dans l'appli).
Met aussi à jour `sdr_switch.py` (redémarrage / arrêt du Pi, journaux,
sauvegarde des configurations, mode « clé arrêtée »).

## Balises de détresse 406 MHz (SATER)

```
curl -fsSL https://raw.githubusercontent.com/F4JCF/STATION-SDR/main/raspberry/install_beacon406.sh | sudo bash
```

Compile le décodeur dec406 (Fabrice Morel, MIT) pour RTL-SDR et installe
`beacon406_api.py` (port 5009). Le module « Balise 406 » s'ajoute à la
bascule de clé SDR : ouvrir la page « Balises de détresse » écoute
406,0–406,1 MHz et décode les balises 1G et 2G (ELT, EPIRB, PLB, essais).
Écoute seule : rien n'est émis.
