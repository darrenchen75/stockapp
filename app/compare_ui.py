"""The compare screen. This module pulls in Qt and the backtest code, so only the lazy compare window imports it."""
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import QDate, QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDateEdit, QDialog,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from app.backtest import (SOURCE, Cancelled, DownloadError, StrategyError, backtest, buy_spy, load_strategy, run,
                          RESULTS_FORMAT)
from app.daily import last_final_date, load_bars
from app.runs import RunRecord, Settings, delete_run, export_run, list_runs, load_result, rename_run, save_run
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
    """Computes SPY buy-and-hold on a thread. Emits (generation, key, Result, error): a Result; or no Result and
    an empty error when SPY has no rows; or no Result and a one-line error."""
    done = Signal(int, object, object, str)

    def __init__(self, db_path: str, generation: int, key: tuple, start: str, end: str):
        super().__init__()
        self.db_path, self.generation, self.key, self.start, self.end = db_path, generation, key, start, end

    def start_thread(self) -> None:
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        store, result, error = None, None, ""
        try:
            store = Store(self.db_path)
            bars = load_bars(store, symbols=["SPY"])
            if not bars.empty:
                result = run(buy_spy, bars, self.start, self.end, BASE, 0.0, True)
        except Exception as e:
            error = one_line(f"{type(e).__name__}: {e}")
        finally:
            if store:
                store.db.close()
        self.done.emit(self.generation, self.key, result, error)  # last statement, after the store is closed


DATE_FORMAT = "yyyy-MM-dd"
COPY_NOTE = "Original file not found; using the copy saved with this run"


class NewRunDialog(QDialog):
    """The fields for one run, and its progress. The window owns the worker and drives this through the methods below."""
    run_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, parent, settings: Settings | None = None, strategy_path: str = "", fallback_path: str = ""):
        super().__init__(parent)
        self.setWindowTitle("New backtest run")
        s = settings or Settings("2016-01-04", last_final_date(datetime.now(timezone.utc)).isoformat(), 100_000.0, 0.0,
                                 True)
        note = ""
        if strategy_path and not Path(strategy_path).is_file() and fallback_path and Path(fallback_path).is_file():
            strategy_path, note = fallback_path, COPY_NOTE
        self.path = QLineEdit(strategy_path)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path, 1)
        path_row.addWidget(browse)
        self.start, self.end = (QDateEdit(QDate.fromString(d, DATE_FORMAT), displayFormat=DATE_FORMAT,
                                          calendarPopup=True) for d in (s.start, s.end))
        self.capital = QDoubleSpinBox(minimum=1, maximum=1e12, decimals=2, value=s.capital, prefix="$")
        self.cost = QDoubleSpinBox(minimum=0, maximum=1000, decimals=2, value=s.cost_bps, suffix=" bps")
        self.dividends = QCheckBox("Include dividends", checked=s.dividends)
        self.fields = QWidget()
        form = QFormLayout(self.fields)
        form.setContentsMargins(0, 0, 0, 0)
        for label, widget in (("Strategy file", path_row), ("Start date", self.start), ("End date", self.end),
                              ("Starting capital", self.capital), ("Cost per trade", self.cost), ("", self.dividends)):
            form.addRow(label, widget)
        self.note_label = QLabel(note, wordWrap=True)
        self.bar = QProgressBar(visible=False)
        self.error = QLabel(wordWrap=True)
        self.error.setStyleSheet("color: #c62828")
        self.run_button = QPushButton("Run", default=True)
        self.cancel_button = QPushButton("Cancel")
        self.run_button.clicked.connect(self.request_run)
        self.cancel_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.cancel_button)
        column = QVBoxLayout(self)
        for part in (self.fields, self.note_label, self.bar, self.error):
            column.addWidget(part)
        column.addLayout(buttons)
        self.running = False
        self.setMinimumWidth(460)

    def note(self) -> str:
        return self.note_label.text()

    def values(self) -> tuple[str, Settings]:
        return self.path.text().strip(), Settings(self.start.date().toString(DATE_FORMAT),
                                                  self.end.date().toString(DATE_FORMAT), self.capital.value(),
                                                  self.cost.value(), self.dividends.isChecked())

    def browse(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Strategy file", self.path.text(), "Python files (*.py)")
        if name:
            self.path.setText(name)

    def request_run(self) -> None:
        path, s = self.values()
        if not path:
            self.error.setText("Pick a strategy file")
        elif s.start > s.end:
            self.error.setText("The start date is after the end date")
        else:
            self.error.clear()
            self.run_requested.emit()

    def set_running(self, running: bool) -> None:
        self.running = running
        self.fields.setEnabled(not running)
        self.run_button.setEnabled(not running)
        self.bar.setVisible(running)
        self.bar.setRange(0, 0)  # busy until the first progress arrives
        if running:
            self.error.clear()

    def show_progress(self, stage: str, done: int, total: int) -> None:
        self.bar.setRange(0, total)
        self.bar.setValue(done)
        self.bar.setFormat(f"{'Downloading' if stage == 'download' else 'Running day'} {done} of {total}")

    def show_error(self, text: str) -> None:
        self.error.setText(text)

    def reject(self) -> None:
        if self.running:
            self.cancel_requested.emit()  # the window reports back when the worker has stopped
        else:
            super().reject()


def ask_export(parent, record: RunRecord) -> str:
    """Ask for a folder and export into it. Returns a one-line message, or "" when the user backed out."""
    folder = QFileDialog.getExistingDirectory(parent, "Export portfolio.csv and positions.csv to")
    if not folder:
        return ""
    try:
        export_run(record, folder)
    except Exception as e:
        return one_line(f"Could not export: {e}")
    return f"Exported to {folder}"


def fixed(value: float, spec: str) -> str:
    return "—" if pd.isna(value) else format(value, spec)


class RunDetail(QDialog):
    """Holdings at each rebalance, and the run's warnings."""

    def __init__(self, parent, record: RunRecord):
        super().__init__(parent)
        self.record = record
        self.setWindowTitle(f"{record.name} · {record.id}")
        result = load_result(record)
        self.frame = result.positions
        self.groups = sorted(self.frame["decided_on"].unique())
        self.picker = QComboBox()
        self.picker.addItems([f"Decided {pd.Timestamp(d):%Y-%m-%d}" for d in self.groups])
        self.picker.currentIndexChanged.connect(self.show_group)
        self.positions = QTableWidget()
        self.positions.setEditTriggers(QAbstractItemView.NoEditTriggers)
        first = QWidget()
        column = QVBoxLayout(first)
        column.addWidget(self.picker)
        column.addWidget(self.positions)
        self.warnings = QTableWidget()
        self.warnings.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tabs = QTabWidget()
        self.tabs.addTab(first, "Positions")
        self.tabs.addTab(self.warnings, "Warnings")
        with open(record.folder / "portfolio.csv") as f:  # the note is written by write_results, not composed here
            self.note = QLabel(f.readline().removeprefix("# ").strip(), wordWrap=True)
        self.export_button = QPushButton("Export…")
        self.export_button.clicked.connect(self.export)
        self.status = QLabel()
        footer = QHBoxLayout()
        footer.addWidget(self.status, 1)
        footer.addWidget(self.export_button)
        outer = QVBoxLayout(self)
        outer.addWidget(self.tabs)
        outer.addWidget(self.note)
        outer.addLayout(footer)
        self.resize(820, 520)
        self.fill_warnings(result.portfolio)
        self.show_group(0)

    @staticmethod
    def fill(table: QTableWidget, headers: list[str], rows: list[list[str]], right_from: int = 99) -> None:
        table.clear()
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                cell = QTableWidgetItem(text)
                if c >= right_from:
                    cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(r, c, cell)
        table.resizeColumnsToContents()

    def show_group(self, index: int) -> None:
        rows = []
        if 0 <= index < len(self.groups):
            for _, p in self.frame[self.frame["decided_on"] == self.groups[index]].iterrows():
                rows.append([p["symbol"], fixed(p["target_weight"], ".1%"), fixed(p["fill_price"], ",.2f"),
                             fixed(p["shares"], ",.2f"), fixed(p["shares"] * p["fill_price"], ",.2f"),
                             "" if pd.isna(p["warning"]) else str(p["warning"])])
        self.fill(self.positions, ["Symbol", "Target weight", "Fill price", "Shares", "Value", "Note"], rows, 1)

    def fill_warnings(self, portfolio: pd.DataFrame) -> None:
        found = [(p["decided_on"], p["symbol"], p["warning"]) for _, p in self.frame.iterrows() if pd.notna(p["warning"])]
        found += [(p["date"], "", p["warning"]) for _, p in portfolio.iterrows() if pd.notna(p["warning"])]
        found.sort(key=lambda w: w[0])
        self.fill(self.warnings, ["Date", "Symbol", "Warning"], [[f"{d:%Y-%m-%d}", s, str(w)] for d, s, w in found])

    def export(self) -> None:
        self.status.setText(ask_export(self, self.record))


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
        self.benchmarks: dict[tuple, object] = {}  # (daily_through, start, end) -> Result, or None for no SPY rows
        self.spy_error: tuple | None = None  # (key, message) of the last failed computation; never cached
        self.generation, self.workers, self._updating = 0, {}, False  # workers: generation -> worker
        self.run_worker: RunWorker | None = None  # held until its terminal signal arrives, or it could be collected
        self.run_dialog: NewRunDialog | None = None
        self.detail: RunDetail | None = None

        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.show_menu)
        self.list.itemChanged.connect(self.on_item_changed)
        self.list.currentRowChanged.connect(self.update_buttons)
        self.list.itemDoubleClicked.connect(self.on_double_click)
        self.new_button, self.rerun_button, self.export_button = (QPushButton(t) for t in ("New…", "Re-run", "Export…"))
        self.new_button.clicked.connect(self.new_run)
        self.rerun_button.clicked.connect(lambda: self.rerun(self.selected().id))
        self.export_button.clicked.connect(lambda: self.export(self.selected().id))
        left = QWidget()
        side = QVBoxLayout(left)
        side.setContentsMargins(0, 0, 0, 0)
        side.addWidget(self.list)
        buttons = QHBoxLayout()
        for button in (self.new_button, self.rerun_button, self.export_button):
            buttons.addWidget(button)
        side.addLayout(buttons)
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
        split.addWidget(left)
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
        current = self.selected()
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
        if current:  # keep the selection (clear() dropped it), so Re-run and Export stay usable
            self.list.setCurrentRow(next((i for i, r in enumerate(self.records) if r.id == current.id), -1))
        self.update_buttons()
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

    def selected(self):
        row = self.list.currentRow()
        return self.records[row] if 0 <= row < len(self.records) else None

    # --- new run, detail, export ---
    @property
    def busy(self) -> bool:
        return self.run_worker is not None

    def update_buttons(self, *_) -> None:
        rec = self.selected()
        self.new_button.setEnabled(not self.busy)
        self.rerun_button.setEnabled(not self.busy and rec is not None and rec.settings is not None)
        self.export_button.setEnabled(rec is not None and rec.readable)

    def _open_dialog(self, dialog: NewRunDialog) -> NewRunDialog:
        if self.run_dialog is not None:
            self.run_dialog.deleteLater()
        self.run_dialog = dialog
        dialog.run_requested.connect(self.start_run)
        dialog.cancel_requested.connect(self.cancel_run)
        dialog.show()
        return dialog

    def new_run(self):
        return None if self.busy else self._open_dialog(NewRunDialog(self))

    def rerun(self, run_id: str):
        rec = self._record(run_id)
        if self.busy or rec.settings is None:
            return None
        return self._open_dialog(NewRunDialog(self, rec.settings, rec.strategy_path, str(rec.folder / "strategy.py")))

    def start_run(self) -> None:
        dialog = self.run_dialog
        if self.busy or dialog is None:
            return
        path, s = dialog.values()
        worker = RunWorker(path, s, self.make_clients, self.db_path, self.root, self.app_version)
        worker.progressed.connect(dialog.show_progress)
        worker.finished.connect(self.on_run_finished)
        worker.failed.connect(lambda text: self.on_run_ended(text))
        worker.stopped.connect(lambda: self.on_run_ended("Cancelled"))
        self.run_worker = worker
        dialog.set_running(True)
        self.update_buttons()
        worker.start()

    def cancel_run(self) -> None:
        if self.run_worker:
            self.run_worker.cancel()

    def closeEvent(self, event):
        if self.busy:
            if QMessageBox.question(self, "Compare backtests", "A backtest is running. Stop it and quit?") != QMessageBox.Yes:
                event.ignore()
                return
            self.cancel_run()
        super().closeEvent(event)

    def on_run_ended(self, text: str) -> None:
        self.run_worker = None  # the worker has finished its cleanup: it signals last
        self.run_dialog.set_running(False)
        self.run_dialog.show_error(text)
        self.update_buttons()

    def on_run_finished(self, record) -> None:
        self.run_worker = None
        self.run_dialog.set_running(False)
        self.run_dialog.accept()
        self.status.setText(f"Saved {record.name}")
        self.reload()  # also refreshes the buttons

    def on_double_click(self, item: QListWidgetItem) -> None:
        row = self.list.row(item)
        if 0 <= row < len(self.records) and self.records[row].readable:
            try:
                self.detail = RunDetail(self, self.records[row])
            except Exception as e:
                self.status.setText(one_line(f"Could not open {self.records[row].name}: {e}"))
                return
            self.detail.show()

    def export(self, run_id: str) -> None:
        self.status.setText(ask_export(self, self._record(run_id)) or self.status.text())

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
            self.spy_error = None  # a failure is retried, not remembered
            self.workers[self.generation] = worker  # referenced until it reports
            worker.done.connect(self.on_benchmark)
            worker.start_thread()
        self.redraw()

    def on_benchmark(self, generation: int, key: tuple, result, error: str) -> None:
        self.workers.pop(generation, None)
        if generation != self.generation:
            return
        if error:
            self.spy_error = key, error
        else:
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
            failed = self.spy_error and self.spy_error[0] == key
            self.spy_note.setText(f"SPY: could not compute ({self.spy_error[1]})" if failed else "SPY: loading…")
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
