import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.trading.client import TradingClient
from PySide6.QtCore import QLockFile, QSortFilterProxyModel, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QActionGroup, QColor, QDesktopServices, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QProgressDialog, QSplitter, QTableWidget, QTableWidgetItem,
                               QTreeView, QVBoxLayout, QWidget)

from app import groups, prefs, quotes, sessions, settings, theme, update
from app.chart import PriceChart
from app.feed import KEEP_DAYS, Poller
from app.listing import Listing, load_listings
from app.store import ET, Store
from app.version import VERSION

log = logging.getLogger(__name__)

COLUMNS = ["Symbol", "Name", "Last", "% Chg", "Volume", "Gaps"]
UP, DOWN = QColor("#26a69a"), QColor("#ef5350")
LIVE_SECONDS = 300 + 900  # SIP bars arrive 15 minutes late


class ListProxy(QSortFilterProxyModel):
    """Sorts rows within each parent; group rows stay in name order."""

    def lessThan(self, left, right):
        a, b = left.siblingAtColumn(0).data(Qt.UserRole), right.siblingAtColumn(0).data(Qt.UserRole)
        if a is None or b is None:
            return super().lessThan(left, right)
        # Descending sorts call lessThan(right, left), so flip to keep names ascending.
        return a.name > b.name if self.sortOrder() == Qt.DescendingOrder else a.name < b.name


class StatsPanel(QTableWidget):
    ROWS = ["Name", "Last", "Change 1D", "Change 5D", "Change 30D", "Volume", "Volatility (30D)", "Sector"]

    def __init__(self):
        super().__init__(len(self.ROWS), 2)
        self.setVerticalHeaderLabels(self.ROWS)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.horizontalHeader().setStretchLastSection(True)

    def set_quotes(self, left: quotes.Quote, right: quotes.Quote, names=("", "SPY")):
        self.setHorizontalHeaderLabels(list(names))
        pct = lambda v: None if v is None else f"{v:+.2f}%"
        for col, q in enumerate((left, right)):
            vol = None if q.vol is None else f"{q.vol * 100:.1f}% ({q.vol_n} d)"
            cells = [q.name, None if q.last is None else f"{q.last:,.2f}", pct(q.chg_1d), pct(q.chg_5d),
                     pct(q.chg_30d), None if q.volume is None else f"{q.volume:,.0f}", vol, q.sector]
            for row, text in enumerate(cells):
                self.setItem(row, col, QTableWidgetItem(text or ""))


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
        self.paper = None  # the paper trading window, once opened
        self.store = Store(db_path)
        self.listings = load_listings()
        self.symbol: str | None = None
        self.sessions: list[sessions.Session] = []  # loaded once per refresh; selection reuses them
        self.poller: Poller | None = None

        self.model = QStandardItemModel(0, len(COLUMNS))
        self.model.setHorizontalHeaderLabels(COLUMNS)
        self.model.setHeaderData(COLUMNS.index("Gaps"), Qt.Horizontal, "Trading days with no bars in the last 30 days", Qt.ToolTipRole)
        saved = prefs.load(data_dir)
        self.group_by = saved.get("group_by") if saved.get("group_by") in groups.BY else "none"
        expanded = saved.get("expanded")
        self.expanded = {x for x in expanded if isinstance(x, str)} if isinstance(expanded, list) else set()
        self.shape = None  # the grouping the model was last built for; rebuilt only when it changes
        self.rows: dict[str, list[list[QStandardItem]]] = {}  # symbol -> its rows (several in index view)
        self.group_rows: dict[str, list[QStandardItem]] = {}
        self.proxy = ListProxy()  # re-sorts on data change; source rows stay put
        self.proxy.setSourceModel(self.model)

        self.tree = QTreeView()
        self.tree.setModel(self.proxy)
        self.tree.setUniformRowHeights(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setExpandsOnDoubleClick(False)  # a click already toggles exchange groups
        self.tree.selectionModel().currentRowChanged.connect(self.on_select)
        self.tree.clicked.connect(self.on_click)
        self.tree.expanded.connect(lambda i: self.on_expand(i, True))
        self.tree.collapsed.connect(lambda i: self.on_expand(i, False))
        self.group_by_box = QComboBox()
        self.group_by_box.addItems([b.capitalize() for b in groups.BY])
        self.group_by_box.setCurrentIndex(groups.BY.index(self.group_by))
        self.group_by_box.currentIndexChanged.connect(self.set_group_by)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Group by"))
        bar.addWidget(self.group_by_box)
        self.feed_tag = QLabel()
        bar.addWidget(self.feed_tag)
        bar.addStretch()
        left = QWidget()
        box = QVBoxLayout(left)
        box.setContentsMargins(0, 0, 0, 0)
        box.addLayout(bar)
        box.addWidget(self.tree)

        self.chart = PriceChart()

        self.stats = StatsPanel()
        right = QSplitter(Qt.Vertical)
        right.addWidget(self.chart)
        right.addWidget(self.stats)
        right.setSizes([480, 220])
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([520, 680])
        self.setCentralWidget(split)
        view = self.menuBar().addMenu("View")
        self.theme_actions = {}
        theme_group = QActionGroup(self)
        current = saved.get("theme", "system")
        for choice in theme.CHOICES:
            action = view.addAction(choice.capitalize(), lambda c=choice: self.set_theme(c))
            action.setCheckable(True)
            action.setChecked(choice == current)
            theme_group.addAction(action)
            self.theme_actions[choice] = action
        tools = self.menuBar().addMenu("Tools")
        tools.addAction("Backtests…", self.open_backtests)
        tools.addAction("Paper trading…", self.open_paper)

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
                             trading=TradingClient(*keys, paper=True),
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

    def make_clients(self):
        return StockHistoricalDataClient(*self.keys), CorporateActionsClient(*self.keys)

    def open_backtests(self):
        if self.compare is None:  # imported here so the viewer never loads the backtest code
            from app.compare_ui import CompareWindow
            from app.paper_state import paper_dir
            from app.runs import runs_dir

            def paper_source():  # runs on the compare window's worker thread, each time it asks
                from app.paper import account_key, make_broker, paper_lines, track_fills
                from app.paper_state import AccountStore

                current = paper_dir() / "current.json"  # the last paper account confirmed in the paper window
                if not self.keys or not current.exists():
                    return []
                key = json.loads(current.read_text())["account_key"]
                broker = make_broker(self.keys)
                if (found := account_key(broker.account())) != key:
                    raise ValueError(f"the keys point to {found}, not the confirmed {key}")
                acct, store = AccountStore(paper_dir(), key), Store(self.db_path)
                try:
                    track_fills(broker, acct, store)
                finally:
                    store.db.close()
                return paper_lines(acct, broker.equity_history())

            self.compare = CompareWindow(self.db_path, runs_dir(), self.make_clients, VERSION,
                                         paper_source=paper_source)
        self.compare.show()
        self.compare.raise_()
        self.compare.activateWindow()

    def open_paper(self):
        if self.paper is None:  # imported here so the viewer never loads the paper code
            from app.paper import make_broker
            from app.paper_state import paper_dir
            from app.paper_ui import PaperWindow

            self.paper = PaperWindow(self.db_path, paper_dir(), (lambda: make_broker(self.keys)) if self.keys else None,
                                     self.make_clients, VERSION)
        self.paper.show()
        self.paper.raise_()
        self.paper.activateWindow()

    def offer_update(self, release: update.Release):
        if any(w is not None and w.busy for w in (self.compare, self.paper)):
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

    def set_theme(self, choice: str) -> None:
        theme.apply(QApplication.instance(), choice)
        prefs.save(self.data_dir, theme=choice)

    def set_group_by(self, i: int):
        self.group_by = groups.BY[i]
        prefs.save(self.data_dir, group_by=self.group_by)
        self.refresh()

    def on_expand(self, index, is_open: bool):
        name = index.siblingAtColumn(0).data(Qt.UserRole).name
        new = self.expanded | {name} if is_open else self.expanded - {name}
        if new != self.expanded:  # rebuilds re-expand saved groups; no write for those
            self.expanded = new
            prefs.save(self.data_dir, expanded=sorted(new))

    def on_click(self, index):
        index = index.siblingAtColumn(0)
        g = index.data(Qt.UserRole)
        if g is not None and g.etf is None:  # ETF-backed groups chart their ETF via on_select instead
            self.tree.setExpanded(index, not self.tree.isExpanded(index))

    def rebuild(self, built: list[groups.Group]):
        self.model.removeRows(0, self.model.rowCount())
        self.rows, self.group_rows = {}, {}

        def symbol_row(s):
            row = [QStandardItem() for _ in COLUMNS]
            row[0].setText(s)
            row[1].setText(self.listings[s].name if s in self.listings else "")
            self.rows.setdefault(s, []).append(row)
            return row
        for s in ([] if built else self.symbols):
            self.model.appendRow(symbol_row(s))
        for g in built:
            row = [QStandardItem() for _ in COLUMNS]
            row[0].setText(f"{g.name} ({len(g.members)})")
            row[0].setData(g, Qt.UserRole)
            for s in g.members:
                row[0].appendRow(symbol_row(s))
            self.group_rows[g.name] = row
            self.model.appendRow(row)
        self.tree.setRootIsDecorated(bool(built))
        for g in built:
            if g.name in self.expanded:
                self.tree.setExpanded(self.proxy.mapFromSource(self.group_rows[g.name][0].index()), True)
        if built:
            self.tree.resizeColumnToContents(0)
        for row in self.rows.get(self.symbol, []):  # keep the highlight if its row is visible
            index = self.proxy.mapFromSource(row[0].index())
            if not index.parent().isValid() or self.tree.isExpanded(index.parent()):
                self.tree.setCurrentIndex(index)
                break

    def refresh(self):
        self.sessions = sessions.load(self.store)
        summaries, gaps = self.store.summary(self.sessions), self.store.gaps(self.symbols)
        pcts = {s: x.pct for s, x in summaries.items()}
        listed = {s: self.listings[s] for s in self.symbols if s in self.listings}
        built = groups.build(self.group_by, listed, self.store.exchanges(), pcts)
        shape = (self.group_by, [(g.name, g.members) for g in built])
        if shape != self.shape:
            self.shape = shape
            self.rebuild(built)
        pct_col = COLUMNS.index("% Chg")

        def paint(item, pct):
            item.setData(None if pct is None else round(pct, 2), Qt.DisplayRole)  # None = blank cell
            item.setData(None if pct is None else (UP if pct >= 0 else DOWN), Qt.ForegroundRole)
        for symbol, rows in self.rows.items():
            x = summaries.get(symbol)
            # None (no data) = blank cell
            cells = {"Last": round(x.last, 2) if x else None, "Volume": x.volume if x else None,
                     "Gaps": gaps.get(symbol)}
            for row in rows:
                for name, value in cells.items():
                    row[COLUMNS.index(name)].setData(value, Qt.DisplayRole)
                paint(row[pct_col], pcts.get(symbol))
        for g in built:
            self.group_rows[g.name][0].setData(g, Qt.UserRole)
            paint(self.group_rows[g.name][pct_col], g.avg_pct)
        self.model.setHeaderData(COLUMNS.index("Last"), Qt.Horizontal, self.feed_label(), Qt.ToolTipRole)
        self.feed_tag.setText(self.feed_label())
        self.show_status()
        self.draw()

    def feed_label(self) -> str:
        # No bars yet is a fresh install: the poller marks it SIP and never migrates.
        iex = self.store.meta("feed") != "sip" and self.store.latest() is not None
        return "IEX, switching to SIP…" if iex else "SIP, 15 min delayed"

    def show_status(self):
        ts = self.store.latest()
        if ts is None:
            self.statusBar().showMessage(f"No data yet · {self.feed_label()}")
            return
        when = datetime.fromtimestamp(ts, ET).strftime("%a %b %d %H:%M ET")
        state = "Live" if time.time() - ts < LIVE_SECONDS else "Market closed"
        self.statusBar().showMessage(f"{state} · last bar {when} · {self.feed_label()}")

    def on_select(self, current, _previous):
        if not current.isValid():
            return
        index = current.siblingAtColumn(0)
        g = index.data(Qt.UserRole)
        if g is not None and g.etf is None:
            return  # exchange groups have no chart; a click toggles them
        symbol = g.etf if g else index.data()
        if symbol != self.symbol:  # a rebuild re-selecting the same symbol is drawn once, by refresh
            self.symbol = symbol
            self.draw()

    def draw(self):
        if self.symbol is None:
            return
        sess = self.sessions
        # Always 30 days (+1 for DST slack); the chart's range only picks the view.
        since = (self.store.session_start(self.symbol) or 0) - 31 * 86400
        name = self.listings[self.symbol].name if self.symbol in self.listings else ""
        self.chart.set_data(self.symbol, f"{self.symbol} · {name}" if name else self.symbol, self.store.bars(self.symbol, since),
                            sess, self.feed_label())
        today, quote_since = datetime.now(ET).date(), int(time.time()) - KEEP_DAYS * 86400

        def q(sym):
            lst = self.listings.get(sym) or Listing(sym, "", "", frozenset())
            return quotes.quote(lst, self.store.bars(sym, quote_since), sess, today)
        self.stats.set_quotes(q(self.symbol), q("SPY"), (self.symbol, "SPY"))

    def closeEvent(self, event):
        if self.updating:  # quitting mid-swap could leave no app installed
            event.ignore()
            return
        if self.paper is not None:
            text = ("Paper orders are being sent or cancelled. Quit anyway? Unfinished orders are checked the next "
                    "time you open Paper trading.")
            if self.paper.busy and QMessageBox.question(self, "Stock Bars", text) != QMessageBox.Yes:
                event.ignore()
                return
            self.paper.hide()  # a visible paper window would keep the app running
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
