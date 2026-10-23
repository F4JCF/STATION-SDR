"""Version de l'application STATION-SDR.

Ce fichier est la source de vérité pour la version. Pour publier une
nouvelle version : mettre à jour __version__ ICI, committer et pousser sur
main. Le tag git (ex: v1.0.1) est créé et poussé automatiquement par le
workflow GitHub Actions tag-on-version.yml, qui déclenche ensuite build.yml
pour compiler et publier la Release — aucune étape manuelle n'est requise.
"""

__version__ = "1.23.0"
