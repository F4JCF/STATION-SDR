"""Point d'entrée pour PyInstaller (et pour lancer l'app en développement)."""

import faulthandler
import sys

if sys.stderr is not None:          # en cas de plantage natif : trace du fil fautif
    try:
        faulthandler.enable(all_threads=False)
    except (RuntimeError, ValueError, OSError):
        pass

from station_sdr.main import main  # noqa: E402

if __name__ == "__main__":
    main()
