"""The compare screen. This module pulls in Qt and the backtest code, so only the lazy compare window imports it."""
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal

from app.backtest import Cancelled, DownloadError, StrategyError, backtest, load_strategy
from app.runs import Settings, save_run
from app.store import Store


def one_line(text: str) -> str:
    return " ".join(text.split())


class RunWorker(QObject):
    """Runs one backtest on a thread and saves it. Signals queue onto the thread of whoever listens."""
    progressed = Signal(str, int, int)  # stage "download" or "run", done, total
    finished = Signal(object)  # a RunRecord
    failed = Signal(str)  # one line
    stopped = Signal()

    def __init__(self, strategy_path: str, settings: Settings, make_clients: Callable[[], tuple], db_path: str,
                 root: Path, app_version: str):
        super().__init__()
        self.strategy_path, self.settings, self.make_clients = strategy_path, settings, make_clients
        self.db_path, self.root, self.app_version = db_path, Path(root), app_version
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.busy:
            return
        self._cancel.clear()
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _work(self) -> None:
        store, scratch = None, None
        try:
            data = Path(self.strategy_path).read_bytes()  # read once: the code that runs is the code saved
            self.root.mkdir(parents=True, exist_ok=True)
            # a hidden folder, so list_runs skips it; importing the copy also writes a __pycache__ beside it
            scratch = Path(tempfile.mkdtemp(prefix=".strategy-", dir=self.root))
            (scratch / "strategy.py").write_bytes(data)
            strategy = load_strategy(str(scratch / "strategy.py"))
            store = Store(self.db_path)  # sqlite3 connections belong to the thread that made them
            client, actions = self.make_clients()
            s, cancelled = self.settings, self._cancel.is_set
            result = backtest(strategy, s.start, s.end, s.capital, s.cost_bps, s.dividends, client, actions, store,
                              download_progress=lambda done, total: self.progressed.emit("download", done, total),
                              progress=lambda done, total: self.progressed.emit("run", done, total),
                              cancelled=cancelled)
            record = save_run(self.root, result, s, self.strategy_path, data, store.daily_through(),
                              self.app_version, cancelled=cancelled)
            self.finished.emit(record)
        except Cancelled:
            self.stopped.emit()
        except (StrategyError, DownloadError, ValueError) as e:
            self.failed.emit(one_line(str(e)))
        except Exception as e:  # e.g. an unreadable strategy file or a full disk: the screen still needs an answer
            self.failed.emit(one_line(f"{type(e).__name__}: {e}"))
        finally:
            if store:
                store.db.close()
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)
