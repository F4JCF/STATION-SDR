# STATION-SDR

Tableau de bord Windows pour le Raspberry Pi multi-fonctions **F4JCF-SDR-STATION**
(ADS-B, météo locale, services système, récepteurs SDR).

## Fonctionnalités

- **Météo** — relevé local (`meteo_local.py`, port 5001)
- **Trafic aérien (ADS-B)** — liste des avions détectés (`readsb`/`tar1090`, port 8504)
- **Services** — état des services système : AIS, Direwolf, météo locale, OpenWebRX,
  readsb, gestionnaire SDR, SvxLink (`station_api.py`, port 5003)
- **Récepteurs SDR** — quel récepteur est utilisé par quelle fonction
  (`sdr_manager.py`, port 5002)
- **Mise à jour automatique** — au démarrage, l'application vérifie s'il existe
  une version plus récente sur GitHub (Releases) et propose de l'installer

Rafraîchissement automatique toutes les 15 secondes (réglable).

## Installation

1. Télécharger `STATION-SDR.exe` depuis les
   [Releases](https://github.com/F4JCF/STATION-SDR/releases) du dépôt.
2. Le lancer — aucune installation de Python n'est nécessaire.
3. Dans le menu **STATION-SDR → Paramètres**, vérifier/ajuster :
   - l'adresse IP du Raspberry Pi (par défaut `192.168.1.229`)
   - (optionnel) un jeton GitHub personnel, voir ci-dessous

## Mise à jour automatique et dépôt privé

Le dépôt `F4JCF/STATION-SDR` est **privé**. Télécharger un fichier de release
privée nécessite une authentification GitHub, même en lecture seule. Pour que
la vérification/mise à jour automatique fonctionne, renseigner un jeton
d'accès personnel GitHub (*Personal Access Token*, droit `repo` en lecture
suffit) dans **Paramètres → Jeton GitHub**.

- Sans jeton : l'application fonctionne normalement, seule la vérification de
  mise à jour est ignorée silencieusement.
- Avec jeton : au démarrage, l'application compare la version installée à la
  dernière release GitHub et propose d'installer automatiquement la nouvelle
  version si elle est plus récente.

Le jeton n'est jamais intégré au code ni au dépôt : il reste uniquement dans
le fichier de configuration local de l'utilisateur
(`%APPDATA%\STATION-SDR\config.json`).

## Publier une nouvelle version (pour le mainteneur)

1. Mettre à jour `__version__` dans `station_sdr/version.py`.
2. Committer, puis créer et pousser un tag correspondant :
   ```
   git tag v1.0.1
   git push origin v1.0.1
   ```
3. Le workflow GitHub Actions (`.github/workflows/build.yml`) compile
   automatiquement `STATION-SDR.exe` avec PyInstaller et publie une Release
   GitHub avec l'exécutable en pièce jointe.

## Développement local

```
pip install -r requirements.txt
python run.py
```

## Licence

MIT — voir [LICENSE](LICENSE).
