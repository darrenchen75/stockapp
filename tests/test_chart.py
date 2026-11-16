import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import date, timedelta

import pytest
from PySide6.QtWidgets import QApplication

from app.chart import PriceChart
from app.sessions import Session, to_x
from app.store import Bar
from tests.test_store import ts

DAYS = [date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13)]
S = [Session(d, ts(d.year, d.month, d.day, 9, 30), ts(d.year, d.month, d.day, 16, 0)) for d in DAYS]
bars = [Bar("A", ts(d.year, d.month, d.day, 8, 0) + 300 * i, 10, 10 + i % 3, 9, 10 + i % 2, 100)
        for d in DAYS for i in range(108)]  # 08:00-17:00 ET every 5 min
later_bar = Bar("A", ts(2026, 11, 13, 17, 30), 11, 11, 11, 11, 100)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def pan(c, lo, hi):
    c.vb.setXRange(lo, hi)
    c.vb.sigRangeChangedManually.emit([True, False])


def test_range_button_resets_a_panned_view(qapp):
    c = PriceChart(); c.set_data("A", "A · Alpha", bars, S, "SIP, 15 min delayed")
    pan(c, 0, 10)
    assert c.hold is not None
    c.set_range("1D")
    assert c.hold is None and c.visible_ts()[1] >= bars[-1].ts


def test_held_view_keeps_its_times_after_new_bar_and_prune(qapp):
    c = PriceChart(); c.set_data("A", "A", bars, S, "")
    pan(c, *[to_x(t, []) for t in (bars[10].ts, bars[50].ts)])
    before = c.visible_ts()
    c.set_data("A", "A", bars[5:] + [later_bar], S, "")
    assert c.visible_ts() == before


def test_unheld_view_follows_latest_bar(qapp):
    c = PriceChart(); c.set_data("A", "A", bars, S, "")
    c.set_data("A", "A", bars + [later_bar], S, "")
    assert c.visible_ts()[1] >= later_bar.ts


@pytest.mark.parametrize("last_day, name, start_day", [
    (date(2026, 11, 16), "30D", date(2026, 10, 19)),  # Mon; 30 days back is Sat 10-17 -> Mon 10-19
    (date(2026, 11, 19), "5D", date(2026, 11, 16)),   # Thu; 5 days back is Sat 11-14 -> Mon 11-16
    (date(2026, 11, 30), "5D", date(2026, 11, 25)),   # Mon; 5 days back is Wed 11-25, a session
    (date(2026, 11, 3), "5D", date(2026, 10, 29)),    # Tue; 5 days back is Thu 10-29, across the 2026-11-01 DST change
    (date(2026, 3, 10), "5D", date(2026, 3, 5)),      # Tue; 5 days back is Thu 03-05, across the 2026-03-08 DST change
])
def test_range_start_uses_new_york_dates(qapp, last_day, name, start_day):
    days = [d for i in range(41) if (d := last_day - timedelta(days=40 - i)).weekday() < 5
            and d != date(2026, 11, 26)]
    sess = [Session(d, ts(d.year, d.month, d.day, 9, 30), ts(d.year, d.month, d.day, 16, 0)) for d in days]
    data = [Bar("A", ts(d.year, d.month, d.day, 4, 0) + 1800 * i, 10, 10, 10, 10, 1) for d in days for i in range(32)]
    c = PriceChart(); c.set_data("A", "A", data, sess, "")
    c.set_range(name)
    assert c.visible_ts()[0] == ts(start_day.year, start_day.month, start_day.day, 4, 0)


def test_double_click_resets(qapp):
    c = PriceChart(); c.set_data("A", "A", bars, S, "")
    pan(c, 0, 10)
    c.on_double_click()
    assert c.hold is None and c.visible_ts()[1] >= bars[-1].ts


def test_title_axis_labels_and_line_over_candles(qapp):
    c = PriceChart(); c.set_data("AAPL", "AAPL · Apple Inc.", bars, S, "SIP, 15 min delayed")
    assert "AAPL · Apple Inc. · 1D" in c.plot.plotItem.titleLabel.text
    assert c.plot.getAxis("left").labelText == "Price (USD)"
    assert c.line.opts["pen"].width() >= 2 and c.candles.opacity() < 0.5


def test_title_omits_empty_parts_and_line_breaks_at_gaps(qapp):
    import numpy as np
    c = PriceChart(); c.set_data("A", "A", bars, S, "")
    assert c.plot.plotItem.titleLabel.text == "A · 1D"
    _, y = c.line.getData()
    assert np.isnan(y).any()  # bars 5 min apart: never joined across a gap


def test_axis_labels_times_inside_a_day_and_dates_across_days(qapp):
    c = PriceChart(); c.set_data("A", "A", bars, S, "")
    axis = c.plot.getAxis("bottom")
    x = to_x(ts(2026, 11, 13, 10, 0), [])
    axis.setRange(x - 60, x + 60)
    assert axis.tickStrings([x], 1, 60) == ["10:00"]
    axis.setRange(x - 2000, x + 60)
    assert axis.tickStrings([x], 1, 960) == ["Nov 13"]


def test_main_window_charts_30_days_of_the_selected_symbol(qapp, tmp_path, monkeypatch):
    from app import ui
    from app.store import Store
    monkeypatch.setattr(ui.MainWindow, "start_poller", lambda self, keys: None)
    monkeypatch.setattr(ui.MainWindow, "check_for_update", lambda self: None)
    old = Bar("A", ts(2026, 10, 12, 10, 0), 10, 10, 10, 10, 1)  # 32 days before the newest bar: not loaded
    Store(str(tmp_path / "bars.db")).upsert([old, Bar("A", ts(2026, 10, 14, 10, 0), 10, 10, 10, 10, 1), *bars])
    w = ui.MainWindow(str(tmp_path / "bars.db"), ["A"], ("k", "s"), tmp_path)
    w.symbol = "A"; w.draw()
    assert w.chart.bars[0].ts == ts(2026, 10, 14, 10, 0) and w.chart.bars[-1] == bars[-1]
    assert w.chart.visible_ts()[0] == ts(2026, 11, 13, 4, 0)  # 1D
    w.close()


@pytest.mark.parametrize("minutes, broken", [((0, 2, 4), False), ((0, 6), True)])
def test_line_bridges_short_gaps_and_breaks_long_ones(qapp, minutes, broken):
    import numpy as np
    data = [Bar("A", ts(2026, 11, 13, 5, 0) + 60 * m, 10, 10, 10, 10, 1) for m in minutes]
    c = PriceChart(); c.set_data("A", "A", data, S, "")
    assert np.isnan(c.line.getData()[1]).any() == broken


def test_held_ends_off_the_axis_snap_to_nearest_bar_and_keep_it_whole(qapp):
    c = PriceChart(); c.set_data("A", "A", bars, S, "")
    c.hold = (ts(2026, 11, 11, 3, 0), ts(2026, 11, 14, 10, 0))  # before 04:00; a Saturday
    c.set_data("A", "A", bars, S, "")
    assert c.visible_ts() == (bars[0].ts, bars[-1].ts + 60)


def test_full_extended_day_still_gets_hourly_ticks(qapp):
    data = [Bar("A", ts(2026, 11, 13, 4, 0) + 60 * m, 10, 10, 10, 10, 1) for m in range(960)]  # last bar 19:59
    c = PriceChart(); c.set_data("A", "A", data, S, "")
    lo, hi = c.vb.viewRange()[0]
    axis = c.plot.getAxis("bottom"); axis.setRange(lo, hi)
    (step, values), = axis.tickValues(lo, hi, 800)
    assert step < 960 and axis.tickStrings(values[:1], 1, step) == ["04:00"]
