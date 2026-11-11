import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pyqtgraph as pg
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from PySide6.QtCore import QLockFile, QPointF, QRectF, QSortFilterProxyModel, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPicture, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QHBoxLayout, QMainWindow,
                               QMessageBox, QProgressDialog, QPushButton, QSplitter, QTableView,
                               QVBoxLayout, QWidget)

from app import settings, update
from app.feed import Poller
from app.store import ET, Bar, Store
from app.version import VERSION

log = logging.getLogger(__name__)

COLUMNS = ["Symbol", "Last", "% Chg", "Volume", "Gaps"]
RANGES = {"1D": None, "5D": 5, "30D": 30}  # None = since the start of the latest session
UP, DOWN = QColor("#26a69a"), QColor("#ef5350")
LIVE_SECONDS = 300


def update_lock(data_dir: Path) -> QLockFile:
    return QLockFile(str(data_dir / "update.lock"))


def clean_after_update(data_dir: Path):
    lock = update_lock(data_dir)
    if not lock.tryLock(0):  # an update is running; the old copy and its files are still in use
        return
    try:
        update.clean_confirmations(data_dir)
        update.cleanup(update.target(sys.platform, sys.executable, os.environ), sys.platform)
    finally:
        lock.unlock()


class Candles(pg.GraphicsObject):
    """Minute candles drawn once into a QPicture (pyqtgraph's custom-item pattern)."""

    def __init__(self, bars: list[Bar]):
        super().__init__()
        self.bounds = QRectF(bars[0].ts, min(b.l for b in bars),
                             bars[-1].ts + 60 - bars[0].ts,
                             max(b.h for b in bars) - min(b.l for b in bars))
        self.picture = QPicture()
        p = QPainter(self.picture)
        for b in bars:
            color = UP if b.c >= b.o else DOWN
            p.setPen(pg.mkPen(color))
            p.setBrush(pg.mkBrush(color))
            mid = b.ts + 30
            p.drawLine(QPointF(mid, b.l), QPointF(mid, b.h))
            p.drawRect(QRectF(mid - 20, b.o, 40, b.c - b.o))
        p.end()

    def paint(self, painter, *args):
        painter.drawPicture(0, 0, self.picture)

    def boundingRect(self):
        return self.bounds


class UpdateProgress(QProgressDialog):
    """Busy dialog that can't be dismissed while the app replaces itself."""

    def __init__(self, parent):
        super().__init__("Updating Stock Bars…", None, 0, 0, parent)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumDuration(0)

    def reject(self):  # Escape
        pass

    def closeEvent(self, event):
        event.ignore()


class MainWindow(QMainWindow):
    # The poller calls these from its thread; Qt queues them onto the UI thread.
    progressed = Signal(int, int)
    polled = Signal()
    status_changed = Signal(str)
    key_rejected = Signal()
    # Update worker threads emit these.
    update_found = Signal(object)
    update_launched = Signal()
    update_done = Signal(object)
    poller_resume = Signal()
    poller_joined = Signal()

    def __init__(self, db_path: str, symbols: list[str], keys: tuple[str, str], data_dir: Path):
        super().__init__()
        self.setWindowTitle("Stock Bars")
        self.db_path, self.symbols, self.keys, self.data_dir = db_path, symbols, keys, data_dir
        self.declined: set[str] = set()
        self.lock = update_lock(data_dir)
        self.progress: QProgressDialog | None = None
        self.resume_requested = False
        self.updating = False
        self.compare = None  # the backtest compare window, once opened
        self.store = Store(db_path)
        self.symbol: str | None = None
        self.range = "1D"
        self.poller: Poller | None = None

        self.model = QStandardItemModel(0, len(COLUMNS))
        self.model.setHorizontalHeaderLabels(COLUMNS)
        self.model.setHeaderData(COLUMNS.index("Gaps"), Qt.Horizontal, "Trading days with no bars in the last 30 days", Qt.ToolTipRole)
        for s in symbols:
            self.model.appendRow([QStandardItem(s)] + [QStandardItem() for _ in COLUMNS[1:]])
        self.rows = {s: i for i, s in enumerate(symbols)}
        self.proxy = QSortFilterProxyModel()  # re-sorts on data change; source rows stay put
        self.proxy.setSourceModel(self.model)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.selectionModel().currentRowChanged.connect(self.on_select)

        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        buttons = QHBoxLayout()
        group = QButtonGroup(self)
        for name in RANGES:
            button = QPushButton(name, checkable=True, checked=name == self.range)
            button.clicked.connect(lambda _=False, n=name: self.set_range(n))
            group.addButton(button)
            buttons.addWidget(button)
        buttons.addStretch()
        right = QWidget()
        column = QVBoxLayout(right)
        column.addLayout(buttons)
        column.addWidget(self.plot)

        split = QSplitter()
        split.addWidget(self.table)
        split.addWidget(right)
        split.setSizes([520, 680])
        self.setCentralWidget(split)
        self.menuBar().addMenu("Tools").addAction("Backtests…", self.open_backtests)

        # Bound methods of QObjects on the UI thread: Qt queues these cross-thread calls.
        self.progressed.connect(self.on_progress)
        self.polled.connect(self.refresh)
        self.status_changed.connect(self.statusBar().showMessage)
        self.key_rejected.connect(self.on_auth_failed)
        self.update_found.connect(self.offer_update)
        self.update_launched.connect(self.on_update_launched)
        self.update_done.connect(self.on_update_done)
        self.poller_resume.connect(self.on_poller_resume)
        self.poller_joined.connect(self.restart_poller)
        self.refresh()
        self.start_poller(keys)

        # A freshly updated copy starts while the old one still holds the lock.
        QTimer.singleShot(60_000, self.clean_after_update)
        if VERSION != "dev":
            self.update_timer = QTimer(self, interval=24 * 3600 * 1000)
            self.update_timer.timeout.connect(self.check_for_update)
            self.update_timer.start()
            self.check_for_update()

    def start_poller(self, keys: tuple[str, str]):
        self.poller = Poller(StockHistoricalDataClient(*keys), self.db_path, self.symbols,
                             actions=CorporateActionsClient(*keys),
                             on_progress=self.progressed.emit, on_updated=self.polled.emit,
                             on_status=self.status_changed.emit, on_auth_failed=self.key_rejected.emit)
        self.poller.start()

    def on_progress(self, done: int, total: int):
        self.statusBar().showMessage(f"Fetching bars… {done}/{total}")

    def on_auth_failed(self):
        if self.updating:
            # A new poller now would race apply's; the restarted poller re-reports this after a rollback.
            log.warning("key rejected during an update; asking again after it ends")
            return
        keys = settings.ask_keys(self, "Alpaca rejected that key. Check it and try again.")
        if keys is None:
            self.close()
            return
        settings.remember(self, keys)
        self.keys = keys
        self.start_poller(keys)

    def clean_after_update(self):
        clean_after_update(self.data_dir)

    def check_for_update(self):
        threading.Thread(target=self.find_update, daemon=True).start()

    def find_update(self):
        release = update.latest(VERSION)
        if release is not None:
            self.update_found.emit(release)

    def open_backtests(self):
        if self.compare is None:  # imported here so the viewer never loads the backtest code
            from app.compare_ui import CompareWindow
            from app.runs import runs_dir

            def make_clients():
                return StockHistoricalDataClient(*self.keys), CorporateActionsClient(*self.keys)

            self.compare = CompareWindow(self.db_path, runs_dir(), make_clients, VERSION)
        self.compare.show()
        self.compare.raise_()
        self.compare.activateWindow()

    def offer_update(self, release: update.Release):
        if self.compare is not None and self.compare.busy:
            return  # offered again on the next daily check
        if release.version in self.declined or not self.lock.tryLock(0):
            return
        box = QMessageBox(QMessageBox.Question, "Stock Bars",
                          f"Stock Bars {release.version} is available (you have {VERSION}). Update now?", parent=self)
        yes = box.addButton("Update", QMessageBox.AcceptRole)
        box.addButton("Later", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not yes:
            self.declined.add(release.version)
            self.lock.unlock()
            return
        target = update.target(sys.platform, sys.executable, os.environ)
        if target is None:
            self.lock.unlock()
            self.show_update_failed("Couldn't update automatically.")
            return
        self.progress = UpdateProgress(self)
        self.progress.show()
        self.resume_requested = False
        self.updating = True
        threading.Thread(target=self.run_update, args=(release, target, self.poller.halt), daemon=True).start()

    def run_update(self, release, target, halt_poller):
        try:
            update.apply(release, target, sys.platform, self.data_dir, halt_poller=halt_poller,
                         resume_poller=self.poller_resume.emit, on_launched=self.update_launched.emit)
        except Exception as e:  # anything, so the slot still unlocks and shows the window
            self.update_done.emit(e)
        else:
            self.update_done.emit(None)

    def on_update_launched(self):
        self.progress.hide()  # hide, not close: closing the last visible window quits the app
        self.hide()

    def on_update_done(self, error: Exception | None):
        self.updating = False
        self.progress.hide()
        self.progress.deleteLater()
        started = getattr(error, "started", False)
        # poller_resume is queued before update_done, so its slot has already run.
        paused = started and not self.resume_requested  # the new copy is still running
        if not paused:
            self.lock.unlock()
        if error is None:
            QApplication.quit()
            return
        log.warning("update failed: %s", error)
        self.show()
        if paused:
            # No poller restart: a second poller would share bars.db. The lock stays held until close,
            # so the new copy can't clean up the only backup and no new update is offered.
            self.statusBar().showMessage("Paused for update")
            self.show_update_failed("Couldn't update automatically.", str(error))
        elif started:
            self.show_update_failed(f"The update didn't start, so you're still on {VERSION}.", str(error))
        else:
            self.show_update_failed("Couldn't update automatically.", str(error))

    def show_update_failed(self, text: str, detail: str = ""):
        box = QMessageBox(QMessageBox.Warning, "Stock Bars", text, parent=self)
        box.setInformativeText(detail)
        page = box.addButton("Open download page", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is page:
            QDesktopServices.openUrl(QUrl(update.RELEASES_PAGE))

    def on_poller_resume(self):
        self.resume_requested = True
        threading.Thread(target=self.join_poller, args=(self.poller.thread,), daemon=True).start()

    def join_poller(self, thread: threading.Thread | None):
        if thread:
            thread.join()  # halt may have timed out mid-batch; never run two pollers on bars.db
        self.poller_joined.emit()

    def restart_poller(self):
        self.start_poller(self.keys)

    def refresh(self):
        summaries, gaps = self.store.summary(), self.store.gaps(self.symbols)
        for symbol, row in self.rows.items():
            x = summaries.get(symbol)
            pct = x.pct if x else None
            values = [round(x.last, 2), None if pct is None else round(pct, 2), x.volume] if x else [None] * 3
            values.append(gaps.get(symbol))  # None (no data) = blank, like the other columns
            for col, value in enumerate(values, start=1):
                self.model.item(row, col).setData(value, Qt.DisplayRole)  # None = blank cell
            color = None if pct is None else (UP if pct >= 0 else DOWN)
            self.model.item(row, 2).setData(color, Qt.ForegroundRole)
        self.show_status()
        self.draw()

    def show_status(self):
        ts = self.store.latest()
        if ts is None:
            self.statusBar().showMessage("No data yet")
            return
        when = datetime.fromtimestamp(ts, ET).strftime("%a %b %d %H:%M ET")
        state = "Live" if time.time() - ts < LIVE_SECONDS else "Market closed"
        self.statusBar().showMessage(f"{state} · last bar {when}")

    def on_select(self, current, _previous):
        if not current.isValid():
            return
        self.symbol = self.model.item(self.proxy.mapToSource(current).row(), 0).text()
        self.draw()

    def set_range(self, name: str):
        self.range = name
        self.draw()

    def draw(self):
        self.plot.clear()
        if self.symbol is None:
            return
        days = RANGES[self.range]
        since = (self.store.session_start(self.symbol) or 0) if days is None else int(time.time()) - days * 86400
        bars = self.store.bars(self.symbol, since)
        self.plot.setTitle(f"{self.symbol} · {self.range}")
        if bars:
            self.plot.addItem(Candles(bars))

    def closeEvent(self, event):
        if self.updating:  # quitting mid-swap could leave no app installed
            event.ignore()
            return
        if self.compare is not None:
            if self.compare.busy:
                answer = QMessageBox.question(self, "Stock Bars", "A backtest is running. Stop it and quit?")
                if answer != QMessageBox.Yes:
                    event.ignore()
                    return
                self.compare.cancel_run()
            self.compare.hide()  # a visible compare window would keep the app running
        if self.poller:
            self.poller.stop.set()
        self.lock.unlock()  # still held only when paused for update; a no-op otherwise
        super().closeEvent(event)
