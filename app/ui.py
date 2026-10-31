import time
from datetime import datetime

import pyqtgraph as pg
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from PySide6.QtCore import QPointF, QRectF, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QPainter, QPicture, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QHBoxLayout, QMainWindow,
                               QPushButton, QSplitter, QTableView, QVBoxLayout, QWidget)

from app import settings
from app.feed import Poller
from app.store import ET, Bar, Store

COLUMNS = ["Symbol", "Last", "% Chg", "Volume"]
RANGES = {"1D": None, "5D": 5, "30D": 30}  # None = since the start of the latest session
UP, DOWN = QColor("#26a69a"), QColor("#ef5350")
LIVE_SECONDS = 300


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


class MainWindow(QMainWindow):
    def __init__(self, db_path: str, symbols: list[str], keys: tuple[str, str]):
        super().__init__()
        self.setWindowTitle("Stock Bars")
        self.db_path, self.symbols = db_path, symbols
        self.store = Store(db_path)
        self.symbol: str | None = None
        self.range = "1D"
        self.poller: Poller | None = None

        self.model = QStandardItemModel(0, len(COLUMNS))
        self.model.setHorizontalHeaderLabels(COLUMNS)
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
        split.setSizes([420, 780])
        self.setCentralWidget(split)

        self.refresh()
        self.start_poller(keys)

    def start_poller(self, keys: tuple[str, str]):
        self.poller = Poller(StockHistoricalDataClient(*keys), self.db_path, self.symbols,
                             actions=CorporateActionsClient(*keys))
        # Bound methods of QObjects on the UI thread: Qt queues these cross-thread calls.
        self.poller.progress.connect(self.on_progress)
        self.poller.updated.connect(self.refresh)
        self.poller.status.connect(self.statusBar().showMessage)
        self.poller.auth_failed.connect(self.on_auth_failed)
        self.poller.start()

    def on_progress(self, done: int, total: int):
        self.statusBar().showMessage(f"Fetching bars… {done}/{total}")

    def on_auth_failed(self):
        keys = settings.ask_keys(self, "Alpaca rejected that key. Check it and try again.")
        if keys is None:
            self.close()
            return
        settings.remember(self, keys)
        self.start_poller(keys)

    def refresh(self):
        summaries = self.store.summary()
        for symbol, row in self.rows.items():
            x = summaries.get(symbol)
            pct = x.pct if x else None
            values = [round(x.last, 2), None if pct is None else round(pct, 2), x.volume] if x else [None] * 3
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
        if self.poller:
            self.poller.stop.set()
        super().closeEvent(event)
