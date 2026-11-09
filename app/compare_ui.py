"""The compare screen. This module pulls in Qt and the backtest code, so only the lazy compare window imports it."""
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable

import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QHBoxLayout, QInputDialog, QLabel, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from app.backtest import (SOURCE, Cancelled, DownloadError, StrategyError, backtest, buy_spy, load_strategy, run,
                          RESULTS_FORMAT)
from app.daily import load_bars
from app.runs import Settings, delete_run, list_runs, load_result, rename_run, save_run
from app.stats import BASE, best, drawdown, growth, summary
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
        outcome = None  # (signal, args), emitted last
        try:
            data = Path(self.strategy_path).read_bytes()  # read once: the code that runs is the code saved
            self.root.mkdir(parents=True, exist_ok=True)
            # a hidden folder, so list_runs skips it; importing the copy also writes a __pycache__ beside it
            scratch = Path(tempfile.mkdtemp(prefix=".strategy-", dir=self.root))
            (scratch / "strategy.py").write_bytes(data)
            try:
                strategy = load_strategy(str(scratch / "strategy.py"))
            except StrategyError as e:  # name the user's file, not the scratch copy
                raise StrategyError(str(e).replace(str(scratch / "strategy.py"), self.strategy_path)) from e
            store = Store(self.db_path)  # sqlite3 connections belong to the thread that made them
            client, actions = self.make_clients()
            s, cancelled = self.settings, self._cancel.is_set
            result = backtest(strategy, s.start, s.end, s.capital, s.cost_bps, s.dividends, client, actions, store,
                              download_progress=lambda done, total: self.progressed.emit("download", done, total),
                              progress=lambda done, total: self.progressed.emit("run", done, total),
                              cancelled=cancelled)
            record = save_run(self.root, result, s, self.strategy_path, data, store.daily_through(),
                              self.app_version, cancelled=cancelled)
            outcome = self.finished, (record,)
        except Cancelled:
            outcome = self.stopped, ()
        except (StrategyError, DownloadError, ValueError) as e:
            outcome = self.failed, (one_line(str(e)),)
        except Exception as e:  # e.g. an unreadable strategy file or a full disk: the screen still needs an answer
            outcome = self.failed, (one_line(f"{type(e).__name__}: {e}"),)
        finally:
            if store:
                store.db.close()
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)
        # last statement, after cleanup: a slot that runs on this signal finds the store closed and the scratch gone
        outcome[0].emit(*outcome[1])


MAX_TICKED = 6
# on the plot's black background; lightness differs as well as hue, so lines stay apart for colour-blind eyes
COLORS = ["#3a6bff", "#ffd24a", "#ef5350", "#4dd0c8", "#b266e0", "#ff8c3a"]
SPY_COLOR = "#9e9e9e"
ROWS = [("total_return", "Total return"), ("cagr", "Yearly growth"), ("worst_drop", "Worst drop"),
        ("rebalances", "Rebalances"), ("warnings", "Warnings")]


def swatch(color: str) -> QIcon:
    pixmap = QPixmap(12, 12)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)


def percent(value: float | None, sign: bool = False) -> str:
    return "—" if value is None else f"{value:{'+' if sign else ''}.1%}"


class BenchmarkWorker(QObject):
    """Computes SPY buy-and-hold on a thread. Emits (generation, key, Result or None when SPY has no usable rows)."""
    done = Signal(int, object, object)

    def __init__(self, db_path: str, generation: int, key: tuple, start: str, end: str):
        super().__init__()
        self.db_path, self.generation, self.key, self.start, self.end = db_path, generation, key, start, end

    def start_thread(self) -> None:
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        store, result = None, None
        try:
            store = Store(self.db_path)
            bars = load_bars(store, symbols=["SPY"])
            if not bars.empty:
                result = run(buy_spy, bars, self.start, self.end, BASE, 0.0, True)
        except Exception:  # no SPY rows in the range, or unreadable data: shown as "no data"
            result = None
        finally:
            if store:
                store.db.close()
        self.done.emit(self.generation, self.key, result)  # last statement, after the store is closed


class CompareWindow(QWidget):
    benchmark_ready = Signal()

    def __init__(self, db_path: str, root: Path, make_clients: Callable[[], tuple], app_version: str):
        super().__init__()
        self.setWindowTitle("Compare backtests")
        self.db_path, self.root, self.make_clients, self.app_version = db_path, Path(root), make_clients, app_version
        self.store = Store(db_path)  # UI thread only, and only for cheap reads
        self.records: list = []  # as returned by list_runs
        self.loaded: dict[str, tuple] = {}  # run id -> (portfolio, positions)
        self.colors: dict[str, str] = {}  # ticked run id -> line colour
        self.mode = "growth"
        self.benchmarks: dict[tuple, object] = {}  # (daily_through, start, end) -> Result, or None for no data
        self.generation, self.workers, self._updating = 0, {}, False  # workers: generation -> worker

        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.show_menu)
        self.list.itemChanged.connect(self.on_item_changed)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(utcOffset=0)})
        self.plot.addLegend()
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.spy_note = QLabel()
        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_note = QLabel(wordWrap=True)
        self.status = QLabel()

        modes = QHBoxLayout()
        self.mode_buttons = {}
        group = QButtonGroup(self)
        for mode, label in (("growth", "Growth of $100,000"), ("drawdown", "Drop from previous high")):
            button = QPushButton(label, checkable=True, checked=mode == self.mode)
            button.clicked.connect(lambda _=False, m=mode: self.set_mode(m))
            group.addButton(button)
            modes.addWidget(button)
            self.mode_buttons[mode] = button
        modes.addStretch()
        modes.addWidget(self.spy_note)
        right = QWidget()
        column = QVBoxLayout(right)
        column.addLayout(modes)
        column.addWidget(self.plot, 3)
        column.addWidget(self.table, 2)
        column.addWidget(self.results_note)
        column.addWidget(self.status)
        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(right)
        split.setSizes([280, 820])
        outer = QHBoxLayout(self)
        outer.addWidget(split)
        self.resize(1150, 750)
        self.reload()

    # --- run list ---
    def reload(self) -> None:
        """Re-read the saved runs, keeping the ticks of runs that still exist."""
        keep = set(self.ticked())
        self.records = list_runs(self.root)
        self.loaded = {k: v for k, v in self.loaded.items() if k in {r.id for r in self.records}}
        self._updating = True
        self.list.clear()
        for rec in self.records:
            s = rec.settings
            text = rec.name if s is None else f"{rec.name}\n{s.start} to {s.end} · {s.cost_bps:g} bps"
            if not rec.readable:
                text += "\n(damaged: cannot be compared)"
            elif rec.format != RESULTS_FORMAT:
                text += "\n(older format)"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, rec.id)
            if rec.readable:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if rec.id in keep else Qt.Unchecked)
            else:
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
            self.list.addItem(item)
        self.spy_item = QListWidgetItem("SPY (buy and hold)")  # pinned last, never counts toward the limit
        self.spy_item.setFlags(self.spy_item.flags() | Qt.ItemIsUserCheckable)
        self.spy_item.setCheckState(Qt.Checked)
        self.spy_item.setIcon(swatch(SPY_COLOR))
        self.list.addItem(self.spy_item)
        self._updating = False
        self.refresh()

    def ticked(self) -> list[str]:
        return [r.id for i, r in enumerate(self.records) if self.list.item(i).checkState() == Qt.Checked]

    def tick(self, i: int) -> None:  # test helper
        if self.list.item(i).flags() & Qt.ItemIsUserCheckable:  # as a click would
            self.list.item(i).setCheckState(Qt.Checked)

    def message(self) -> str:  # test helper
        return self.status.text()

    def on_item_changed(self, item: QListWidgetItem) -> None:
        if self._updating:
            return
        if item is not self.spy_item and item.checkState() == Qt.Checked and len(self.ticked()) > MAX_TICKED:
            self._set_state(item, Qt.Unchecked)
            self.status.setText(f"At most {MAX_TICKED} runs can be compared at once")
            return
        self.refresh()

    def _set_state(self, item: QListWidgetItem, state) -> None:
        self._updating = True
        item.setCheckState(state)
        self._updating = False

    def _record(self, run_id: str):
        return next(r for r in self.records if r.id == run_id)

    def show_menu(self, pos) -> None:
        row = self.list.row(self.list.itemAt(pos)) if self.list.itemAt(pos) else -1
        if not 0 <= row < len(self.records):
            return
        menu = QMenu(self)
        rename, delete = menu.addAction("Rename…"), menu.addAction("Delete…")
        chosen = menu.exec(self.list.mapToGlobal(pos))
        rec = self.records[row]
        if chosen is rename:
            name, ok = QInputDialog.getText(self, "Rename run", "Name:", text=rec.name)
            if ok:
                self.rename_row(row, name)
        elif chosen is delete:
            if QMessageBox.question(self, "Delete run", f"Delete {rec.name} and its saved results?") == QMessageBox.Yes:
                self.delete_row(row)

    def rename_row(self, row: int, name: str) -> bool:
        if not name.strip():
            self.status.setText("A run needs a name")
            return False
        try:
            rename_run(self.records[row], name.strip())
        except Exception as e:
            self.status.setText(one_line(f"Could not rename: {e}"))
            return False
        self.reload()
        return True

    def delete_row(self, row: int) -> bool:
        try:
            delete_run(self.records[row])
        except Exception as e:
            self.status.setText(one_line(f"Could not delete: {e}"))
            return False
        self.reload()
        return True

    # --- chart and table ---
    def set_mode(self, mode: str) -> None:
        if mode not in self.mode_buttons:
            raise ValueError(mode)
        self.mode = mode
        self.mode_buttons[mode].setChecked(True)
        self.redraw()

    def _load(self, run_id: str):
        if run_id not in self.loaded:
            result = load_result(self._record(run_id))
            self.loaded[run_id] = (result.portfolio, result.positions)
        return self.loaded[run_id]

    def _benchmark_key(self):
        ids = self.ticked()
        through = self.store.daily_through()
        if not ids or through is None or self.spy_item.checkState() != Qt.Checked:
            return None
        settings = [self._record(i).settings for i in ids]
        return through, min(s.start for s in settings), max(s.end for s in settings)

    def refresh(self) -> None:
        """Draw the run lines now; the SPY line follows when its computation ends."""
        enabled = self.store.daily_through() is not None
        flags = self.spy_item.flags()
        self._updating = True
        self.spy_item.setFlags(flags | Qt.ItemIsEnabled if enabled else flags & ~Qt.ItemIsEnabled)
        if not enabled:
            self.spy_item.setCheckState(Qt.Unchecked)
        self.spy_item.setToolTip("" if enabled else "No daily data yet: run a backtest first")
        for i, rec in enumerate(self.records):  # a run that cannot be read is unticked, with a message
            item = self.list.item(i)
            if item.checkState() == Qt.Checked:
                try:
                    self._load(rec.id)
                except Exception as e:
                    item.setCheckState(Qt.Unchecked)
                    self.status.setText(one_line(f"Could not read {rec.name}: {e}"))
        self._updating = False
        key = self._benchmark_key()
        self.generation += 1  # any result still on its way is for an older selection
        if key is not None and key not in self.benchmarks:
            worker = BenchmarkWorker(self.db_path, self.generation, key, key[1], key[2])
            self.workers[self.generation] = worker  # referenced until it reports
            worker.done.connect(self.on_benchmark)
            worker.start_thread()
        self.redraw()

    def on_benchmark(self, generation: int, key: tuple, result) -> None:
        self.workers.pop(generation, None)
        if generation != self.generation:
            return
        self.benchmarks[key] = result
        self.redraw()
        if result is not None:
            self.benchmark_ready.emit()

    def redraw(self) -> None:
        ids = self.ticked()
        self.colors = {i: c for i, c in self.colors.items() if i in ids}
        for i in ids:
            if i not in self.colors:
                self.colors[i] = next(c for c in COLORS if c not in self.colors.values())
        self._updating = True
        for i, rec in enumerate(self.records):
            self.list.item(i).setIcon(swatch(self.colors[rec.id]) if rec.id in self.colors else QIcon())
        self._updating = False
        self.plot.clear()
        self.plot.setTitle(None if ids else "Tick a run to compare")
        self.plot.setLabel("left", "Growth of $100,000" if self.mode == "growth" else "Drop from previous high")
        series = growth if self.mode == "growth" else drawdown

        def line(portfolio, color, name, **pen):
            s = series(portfolio)
            x = (s.index - pd.Timestamp("1970-01-01")).total_seconds().to_numpy()  # UTC seconds, as DateAxisItem wants
            self.plot.plot(x, s.to_numpy(), pen=pg.mkPen(color, width=2, **pen), name=name)

        columns = {}  # header -> (summary, colour)
        for i in ids:
            portfolio, positions = self._load(i)
            rec = self._record(i)
            line(portfolio, self.colors[i], rec.name)
            columns[i] = summary(portfolio, positions)
        spy_stats = None
        key = self._benchmark_key()
        if key is None:
            self.spy_note.setText("")
        elif key not in self.benchmarks:
            self.spy_note.setText("SPY: loading…")
        elif self.benchmarks[key] is None:
            self.spy_note.setText("SPY: no data")
        else:
            spy = self.benchmarks[key]
            self.spy_note.setText("")
            line(spy.portfolio, SPY_COLOR, f"SPY (data to {key[0]})", style=Qt.DashLine)
            spy_stats = summary(spy.portfolio, spy.positions)
        self.fill_table(ids, columns, spy_stats)
        dividends = {self._record(i).settings.dividends for i in ids}
        self.results_note.setText(
            "Today's S&P 500 members only, so results have survivorship bias. "
            f"Data: {SOURCE}. Dividends: " + ("mixed" if len(dividends) > 1 else "on" if dividends == {True}
                                              else "off" if dividends else "set per run") + ".")

    def fill_table(self, ids: list[str], columns: dict, spy_stats) -> None:
        marked = best(columns)  # run columns only: SPY is never marked
        shown = [(self._record(i).name, columns[i], self.colors[i], i) for i in ids]
        if spy_stats is not None:
            shown.append(("SPY", spy_stats, SPY_COLOR, None))
        self.table.clear()
        self.table.setRowCount(len(ROWS))
        self.table.setColumnCount(len(shown))
        self.table.setVerticalHeaderLabels([label for _, label in ROWS])
        for col, (name, stats, color, run_id) in enumerate(shown):
            header = QTableWidgetItem(name)
            header.setForeground(QColor(color))
            self.table.setHorizontalHeaderItem(col, header)
            for row, (key, _) in enumerate(ROWS):
                value = stats[key]
                text = percent(value, key == "total_return") if key in ("total_return", "cagr", "worst_drop") \
                    else str(value)
                cell = QTableWidgetItem(text)
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if run_id is not None and marked.get(key) == run_id:
                    font = QFont(cell.font())
                    font.setBold(True)
                    cell.setFont(font)
                self.table.setItem(row, col, cell)
