import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from platformdirs import user_data_dir, user_log_dir
from PySide6.QtWidgets import QApplication

from app import settings, update
from app.feed import load_symbols
from app.ui import MainWindow, clean_after_update

APP = "stockapp"


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--selftest":  # CI runs this on each packaged app
        from app import backtest  # imported here so the viewer never loads the backtest code
        return backtest.selftest(sys.argv[2])
    log_dir = Path(user_log_dir(APP, appauthor=False))
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[RotatingFileHandler(log_dir / "stockapp.log", maxBytes=1_000_000, backupCount=3)],
    )
    data_dir = Path(user_data_dir(APP, appauthor=False))
    data_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    # Confirm first: a slow or missing keychain must not run out the old copy's timeout.
    update.confirm_from_argv(sys.argv)
    clean_after_update(data_dir)
    keys = settings.load_keys()
    if keys is None:
        keys = settings.ask_keys()
        if keys is None:
            return 0
        settings.remember(None, keys)

    window = MainWindow(str(data_dir / "bars.db"), load_symbols(), keys, data_dir)
    window.resize(1200, 700)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
