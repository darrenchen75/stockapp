import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import statistics
from datetime import date, timedelta
from math import sqrt

import pytest
from PySide6.QtWidgets import QApplication

from app.listing import Listing
from app.quotes import Quote, quote
from app.sessions import Session
from app.store import Bar
from tests.test_store import store, ts

L = Listing("A", "Acme", "Tech", frozenset())
TODAY = date(2026, 11, 16)


def days(n):  # weekdays ending 2026-11-13
    out, d = [], date(2026, 11, 13)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def build(ds, closes):
    s = [Session(d, ts(d.year, d.month, d.day, 9, 30), ts(d.year, d.month, d.day, 16, 0)) for d in ds]
    b = [Bar("A", ts(d.year, d.month, d.day, 10, 0), c, c, c, c, 10) for d, c in zip(ds, closes)]
    return b, s


def test_changes_use_calendar_lookback_and_blank_when_short():
    ds = days(40)
    closes = [100 + i for i in range(40)]
    b, s = build(ds, closes)
    q = quote(L, b, s, TODAY)
    ref = lambda lim: closes[max(i for i, d in enumerate(ds) if d <= lim)]
    last = closes[-1]
    assert q.chg_5d == pytest.approx((last / ref(date(2026, 11, 11)) - 1) * 100)
    assert q.chg_30d == pytest.approx((last / ref(date(2026, 10, 17)) - 1) * 100)
    b3, s3 = build(days(3), [1, 2, 3])
    assert quote(L, b3, s3, TODAY).chg_30d is None


def test_volatility_and_sample_size():
    b, s = build(days(4), [100, 101, 99, 100])
    q = quote(L, b, s, TODAY)
    rets = [101 / 100 - 1, 99 / 101 - 1, 100 / 99 - 1]
    assert q.vol_n == 3
    assert q.vol == pytest.approx(statistics.stdev(rets) * sqrt(252))
    b, s = build(days(2), [100, 101])
    assert quote(L, b, s, TODAY).vol is None


def test_pre_post_market_bars_ignored_and_empty():
    b, s = build(days(2), [100, 110])
    b.append(Bar("A", ts(2026, 11, 13, 18, 0), 1, 1, 1, 1, 5))
    assert quote(L, b, s, TODAY).last == 110
    assert quote(L, [], s, TODAY) == Quote("Acme", sector="Tech")


def test_quote_matches_summary_on_same_bars(tmp_path):
    b, s = build(days(5), [100, 101, 99, 100, 103])
    st = store(tmp_path)
    st.upsert(b)
    sm = st.summary(s)["A"]
    q = quote(L, st.bars("A", 0), s, TODAY)
    assert (q.last, q.chg_1d, q.volume) == (sm.last, sm.pct, sm.volume)


def test_panel_blanks_none():
    from app.ui import StatsPanel
    QApplication.instance() or QApplication([])
    p = StatsPanel()
    p.show(quote(L, *build(days(4), [100, 101, 99, 100])[:1], build(days(4), [1] * 4)[1], TODAY), Quote())
    assert p.item(0, 0).text() == "Acme" and p.item(0, 1).text() == ""
    assert p.item(6, 0).text().endswith("(3 d)") and p.item(6, 1).text() == ""
    assert p.item(4, 0).text() == ""  # 30D change missing
