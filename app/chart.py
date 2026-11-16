"""Minute price chart on session-time x (sessions.to_x): a bold close line over faint candles."""
from datetime import datetime, time, timedelta

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QPainter, QPicture
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from app import theme
from app.sessions import DAY_MIN, Session, from_x, holidays, to_x
from app.store import ET, Bar

RANGES = {"1D": None, "5D": 5, "30D": 30}  # None = the latest session's day
LINE_GAP = 5  # trading minutes; shorter gaps (sparse pre/post-market, illiquid stocks) stay joined


class Candles(pg.GraphicsObject):
    """Minute candles drawn once into a QPicture (pyqtgraph's custom-item pattern)."""

    def __init__(self, xs: np.ndarray, o, h, l, c, up: str, down: str):
        super().__init__()
        self.xs, self.lo, self.hi = xs, l, h
        self.bounds = QRectF(xs[0], l.min(), xs[-1] + 1 - xs[0], h.max() - l.min())
        style = {True: (pg.mkPen(up), pg.mkBrush(up)), False: (pg.mkPen(down), pg.mkBrush(down))}
        self.picture = QPicture()
        p = QPainter(self.picture)
        for x, bo, bh, bl, bc in zip(xs, o, h, l, c):
            pen, brush = style[bool(bc >= bo)]
            p.setPen(pen)
            p.setBrush(brush)
            mid = x + 0.5
            p.drawLine(QPointF(mid, bl), QPointF(mid, bh))
            p.drawRect(QRectF(mid - 0.3, bo, 0.6, bc - bo))
        p.end()

    def paint(self, painter, *args):
        painter.drawPicture(0, 0, self.picture)

    def boundingRect(self):
        return self.bounds

    def dataBounds(self, ax, frac=1.0, orthoRange=None):
        """Lets the ViewBox fit the price axis to the candles in view."""
        if ax == 0:
            return self.xs[0], self.xs[-1] + 1
        seen = slice(None) if orthoRange is None else (self.xs + 1 > orthoRange[0]) & (self.xs < orthoRange[1])
        lo, hi = self.lo[seen], self.hi[seen]
        return (lo.min(), hi.max()) if len(lo) else (None, None)


class SessionAxis(pg.AxisItem):
    """Bottom axis in New York time; ticks on the hour within a day, on day starts across days."""

    def __init__(self):
        super().__init__("bottom")
        self.holidays = []

    def tickValues(self, lo, hi, size):
        span = hi - lo
        steps = (15, 30, 60, 120, 240) if span <= DAY_MIN else tuple(DAY_MIN * n for n in (1, 2, 5, 10, 20))
        step = next((s for s in steps if span / s <= max(size / 80, 2)), steps[-1])
        return [(step, list(np.arange(np.ceil(lo / step) * step, hi, step)))]

    def tickStrings(self, values, scale, spacing):
        fmt = "%H:%M" if self.range[1] - self.range[0] <= DAY_MIN else "%b %d"
        return [datetime.fromtimestamp(from_x(v, self.holidays), ET).strftime(fmt) for v in values]


class PriceChart(QWidget):
    def __init__(self):
        super().__init__()
        self.range, self.hold = "1D", None  # hold: the user's zoom/pan, as (start, end) Unix seconds
        self.title = self.feed_label = ""
        self.bars: list[Bar] = []
        self.xs = np.empty(0)
        self.holidays = []
        self.line = self.candles = None
        self.axis = SessionAxis()
        self.plot = pg.PlotWidget(axisItems={"bottom": self.axis})
        self.plot.hideButtons()
        self.plot.setLabel("left", "Price (USD)")
        self.plot.setLabel("bottom", "Time (New York)")
        self.vb = self.plot.getViewBox()
        self.vb.setMouseEnabled(x=True, y=False)
        self.vb.enableAutoRange(axis="y")
        self.vb.setAutoVisible(y=True)
        self.vb.sigRangeChangedManually.connect(self.on_manual)
        self.plot.scene().sigMouseClicked.connect(self.on_click)
        theme.style_plot(self.plot)
        theme.watcher().changed.connect(self.restyle)

        buttons = QHBoxLayout()
        group = QButtonGroup(self)
        self.buttons = {}
        for name in RANGES:
            button = self.buttons[name] = QPushButton(name, checkable=True, checked=name == self.range)
            button.clicked.connect(lambda _=False, n=name: self.set_range(n))
            group.addButton(button)
            buttons.addWidget(button)
        buttons.addStretch()
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addLayout(buttons)
        column.addWidget(self.plot)

    def set_data(self, symbol: str, title: str, bars: list[Bar], sessions: list[Session], feed_label: str) -> None:
        self.title, self.feed_label = title or symbol, feed_label
        self.holidays = self.axis.holidays = holidays(sessions)
        xs = [to_x(b.ts, self.holidays) for b in bars]
        self.bars = [b for b, x in zip(bars, xs) if x is not None]
        self.xs = np.array([x for x in xs if x is not None])
        self.draw()
        if self.hold is None:
            self.scope()
        elif len(self.xs):
            self.vb.setXRange(self.x_near(self.hold[0]), self.x_near(self.hold[1], right=True), padding=0)

    def set_range(self, name: str) -> None:
        self.range = name
        self.buttons[name].setChecked(True)
        self.reset()
        self.show_title()
        self.scope()

    def reset(self) -> None:
        self.hold = None

    def on_double_click(self) -> None:
        self.reset()
        self.scope()

    def on_click(self, ev):
        if ev.double():
            self.on_double_click()

    def on_manual(self, _mask):
        self.hold = self.visible_ts()

    def visible_ts(self) -> tuple[int, int]:
        lo, hi = self.vb.viewRange()[0]
        return from_x(lo, self.holidays), from_x(hi, self.holidays)

    def x_near(self, ts: int, right: bool = False) -> float:
        """x of `ts`; a time off the session axis snaps to the nearest drawn bar (its right edge for a right end)."""
        x = to_x(ts, self.holidays)
        if x is not None:
            return x
        return self.xs[np.abs(np.array([b.ts for b in self.bars]) - ts).argmin()] + right

    def scope(self):
        """Show the chosen range, ending at the newest bar. Start days are New York calendar dates."""
        if not len(self.xs):
            return
        start = datetime.fromtimestamp(self.bars[-1].ts, ET).date()
        if days := RANGES[self.range]:
            start -= timedelta(days=days)
            while start.weekday() >= 5 or start in self.holidays:
                start += timedelta(days=1)
        lo = to_x(int(datetime.combine(start, time(4), ET).timestamp()), self.holidays)
        self.vb.setXRange(lo, self.xs[-1] + 1, padding=0)

    def show_title(self):
        self.plot.setTitle(" · ".join(p for p in (self.title, self.range, self.feed_label) if p))

    def draw(self):
        self.plot.clear()
        self.line = self.candles = None
        self.show_title()
        if not self.bars:
            return
        c = theme.colors()
        o, h, l, close = np.array([(b.o, b.h, b.l, b.c) for b in self.bars], dtype=float).T
        self.candles = Candles(self.xs, o, h, l, close, c.up, c.down)
        self.candles.setOpacity(0.3)
        breaks = np.flatnonzero(np.diff(self.xs) >= LINE_GAP) + 1  # a long gap is a gap, not a slope
        self.line = pg.PlotDataItem(np.insert(self.xs + 0.5, breaks, np.nan), np.insert(close, breaks, np.nan),
                                    pen=pg.mkPen(c.line, width=2), connect="finite")
        self.plot.addItem(self.candles)
        self.plot.addItem(self.line)

    def restyle(self):
        theme.style_plot(self.plot)
        self.draw()
