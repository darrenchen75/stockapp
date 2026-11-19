import logging
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app import prefs, ui
from app.store import Bar
from tests.test_store import ts

SYMBOLS = ["AAPL", "MSFT", "XOM", "SPY", "XLK"]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(ui.MainWindow, "start_poller", lambda self, keys: None)
    monkeypatch.setattr(ui.MainWindow, "check_for_update", lambda self: None)
    made = []

    def make():
        w = ui.MainWindow(str(tmp_path / "bars.db"), SYMBOLS, ("k", "s"), tmp_path)
        made.append(w)
        return w
    yield make
    for w in made:
        w.close()


def top_rows(w):
    m = w.tree.model()
    return [m.index(r, 0).data() for r in range(m.rowCount())]


def group_index(w, name):
    m = w.tree.model()
    return next(m.index(r, 0) for r in range(m.rowCount()) if m.index(r, 0).data(Qt.UserRole).name == name)


def click(w, index):
    w.show()
    QTest.mouseClick(w.tree.viewport(), Qt.LeftButton, pos=w.tree.visualRect(index).center())


def test_flat_mode_lists_every_symbol(window):
    w = window()
    assert w.group_by == "none" and top_rows(w) == sorted(SYMBOLS)
    assert w.tree.model().rowCount(w.tree.model().index(0, 0)) == 0


def test_group_rows_show_count_and_average(window, tmp_path):
    from app.store import Store
    Store(str(tmp_path / "bars.db")).upsert(
        [Bar(s, ts(2026, 11, d, 15, 59), c, c, c, c, 1) for s, d, c in
         [("AAPL", 12, 10), ("AAPL", 13, 11), ("MSFT", 12, 10), ("MSFT", 13, 10.4)]])
    w = window()
    w.group_by_box.setCurrentIndex(1)  # Sector
    assert top_rows(w) == ["Energy (1)", "Information Technology (2)"]
    pct = ui.COLUMNS.index("% Chg")
    tech = group_index(w, "Information Technology").siblingAtColumn(pct)
    assert tech.data() == 7.0 and tech.data(Qt.ForegroundRole) == ui.UP  # (+10% + 4%) / 2
    assert group_index(w, "Energy").siblingAtColumn(pct).data() is None  # XOM has no bars


def test_group_click_charts_etf(window):
    w = window()
    w.group_by_box.setCurrentIndex(1)
    click(w, group_index(w, "Information Technology"))
    assert w.symbol == "XLK" and w.chart.title.startswith("XLK")
    assert w.stats.horizontalHeaderItem(0).text() == "XLK"


def test_exchange_group_click_toggles(window):
    w = window()
    w.group_by_box.setCurrentIndex(2)  # Exchange: nothing cached, so all in Unknown
    unknown = group_index(w, "Unknown")
    assert not w.tree.isExpanded(unknown)
    click(w, unknown)
    assert w.tree.isExpanded(group_index(w, "Unknown")) and w.symbol is None
    click(w, group_index(w, "Unknown"))
    assert not w.tree.isExpanded(group_index(w, "Unknown"))


def test_groups_stay_in_name_order_when_sorting_descending(window):
    w = window()
    w.group_by_box.setCurrentIndex(1)
    w.tree.sortByColumn(0, Qt.DescendingOrder)
    assert top_rows(w) == ["Energy (1)", "Information Technology (2)"]
    tech = group_index(w, "Information Technology")
    m = w.tree.model()
    assert [m.index(r, 0, tech).data() for r in range(m.rowCount(tech))] == ["MSFT", "AAPL"]


def test_grouping_remembered(window, tmp_path):
    w = window()
    w.group_by_box.setCurrentIndex(1)
    w.tree.expand(group_index(w, "Energy"))
    assert prefs.load(tmp_path) == {"group_by": "sector", "expanded": ["Energy"]}
    w.close()
    w2 = window()
    assert w2.group_by == "sector" and w2.group_by_box.currentText() == "Sector"
    assert w2.tree.isExpanded(group_index(w2, "Energy"))
    assert not w2.tree.isExpanded(group_index(w2, "Information Technology"))


def test_refresh_keeps_selection(window):
    w = window()
    w.tree.setCurrentIndex(w.tree.model().index(1, 0))
    w.refresh()
    assert w.tree.currentIndex().data() == "MSFT" and w.symbol == "MSFT"


def double_click(w, index):
    w.show()
    pos = w.tree.visualRect(index).center()
    QTest.mouseClick(w.tree.viewport(), Qt.LeftButton, pos=pos)  # a real double-click starts with a click
    QTest.mouseDClick(w.tree.viewport(), Qt.LeftButton, pos=pos)


def test_double_click_toggles_an_etf_group(window):
    w = window()
    w.group_by_box.setCurrentIndex(1)
    double_click(w, group_index(w, "Information Technology"))
    assert w.symbol == "XLK" and w.tree.isExpanded(group_index(w, "Information Technology"))
    double_click(w, group_index(w, "Information Technology"))
    assert not w.tree.isExpanded(group_index(w, "Information Technology"))


def test_double_click_toggles_an_exchange_group_once(window):
    w = window()
    w.group_by_box.setCurrentIndex(2)
    double_click(w, group_index(w, "Unknown"))
    assert w.tree.isExpanded(group_index(w, "Unknown"))


def test_selection_reuses_the_refreshed_sessions(window, monkeypatch):
    w = window()
    loads = []
    monkeypatch.setattr(ui.sessions, "load", lambda store: loads.append(1) or [])
    w.tree.setCurrentIndex(w.tree.model().index(1, 0))
    w.tree.setCurrentIndex(w.tree.model().index(2, 0))
    assert loads == [] and w.symbol == "SPY"


def test_regroup_draws_once(window, monkeypatch):
    w = window()
    w.tree.setCurrentIndex(w.tree.model().index(0, 0))  # AAPL
    draws = []
    monkeypatch.setattr(w, "draw", lambda: draws.append(w.symbol))
    w.group_by_box.setCurrentIndex(1)
    w.tree.expand(group_index(w, "Information Technology"))
    w.group_by_box.setCurrentIndex(0)  # AAPL's row is visible again and re-selected
    assert draws == ["AAPL", "AAPL"] and w.tree.currentIndex().data() == "AAPL"


def test_failed_settings_save_is_logged_not_raised(window, monkeypatch, caplog):
    w = window()

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(prefs, "save", full)
    with caplog.at_level(logging.WARNING):
        w.set_group_by(1)
    assert w.group_by == "sector" and "No space left" in caplog.text
