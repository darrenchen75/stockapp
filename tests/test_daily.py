from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from alpaca.data.enums import Adjustment, CorporateActionsType, DataFeed
from alpaca.data.timeframe import TimeFrame

from app.daily import (HISTORY_START, IncompleteDownload, build_factors, fetch_daily, last_final_date, load_bars,
                       refresh_daily, universe)
from app.store import ET, DailyBar, Store
from tests.test_feed import HTTPError, utc

NOV5 = date(2026, 11, 5)  # a Thursday
NOW = datetime(2026, 11, 5, 21, 0, tzinfo=ET)


def fake_day(day: str, c=10.0, hour=5):
    # Alpaca stamps daily bars at midnight New York time: 05:00 UTC in winter, 04:00 in summer
    y, m, d = map(int, day.split("-"))
    return SimpleNamespace(timestamp=datetime(y, m, d, hour, tzinfo=timezone.utc),
                           open=c, high=c, low=c, close=c, volume=100.0)


class FakeClient:
    def __init__(self, data=None, errors=()):
        self.data = data or {}  # symbol -> list of fake bars
        self.errors = list(errors)
        self.requests = []

    def get_stock_bars(self, req):
        self.requests.append(req)
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(data={s: self.data[s] for s in req.symbol_or_symbols if s in self.data})


class FakeActions:
    def __init__(self, splits=(), dividends=(), error=None, reverse_splits=()):
        self.splits = list(splits)  # (symbol, ex_date, old_rate, new_rate)
        self.reverse_splits = list(reverse_splits)  # same shape, reported under "reverse_splits"
        self.dividends = list(dividends)  # (symbol, ex_date, rate)
        self.error = error
        self.requests = []

    def get_corporate_actions(self, req):
        self.requests.append(req)
        if self.error:
            raise self.error
        data = {}
        for s, ex, old, new in self.splits:
            data.setdefault("forward_splits", []).append(
                SimpleNamespace(symbol=s, ex_date=ex, old_rate=old, new_rate=new))
        for s, ex, old, new in self.reverse_splits:
            data.setdefault("reverse_splits", []).append(
                SimpleNamespace(symbol=s, ex_date=ex, old_rate=old, new_rate=new))
        for s, ex, rate in self.dividends:
            data.setdefault("cash_dividends", []).append(
                SimpleNamespace(symbol=s, ex_date=ex, rate=rate, special=False))
        return SimpleNamespace(data=data)


def good_client(symbols, day="2020-01-02"):
    return FakeClient({s: [fake_day(day)] for s in symbols})


def row(day, symbol="A", c=10.0):
    return DailyBar(symbol, day, c, c, c, c, 100.0, 1.0, 1.0)


def test_last_final_date_turns_at_2030_new_york():
    assert last_final_date(datetime(2026, 11, 5, 20, 29, tzinfo=ET)) == date(2026, 11, 4)
    assert last_final_date(datetime(2026, 11, 5, 20, 30, tzinfo=ET)) == NOV5


def test_fetch_daily_requests_raw_sip_day_bars():
    client = good_client(["A"])
    fetch_daily(client, FakeActions(), ["A"], NOV5)
    assert client.requests
    assert all(r.adjustment == Adjustment.RAW for r in client.requests)
    assert all(r.feed == DataFeed.SIP and r.timeframe.value == TimeFrame.Day.value for r in client.requests)
    assert all(utc(r.start) == datetime(2016, 1, 1, tzinfo=ET).astimezone(timezone.utc) for r in client.requests)
    # 20:15 on `through`: at the 20:30 cutoff this is already 15 minutes old
    assert all(utc(r.end) == datetime(2026, 11, 5, 20, 15, tzinfo=ET).astimezone(timezone.utc)
               for r in client.requests)


def test_fetch_daily_requests_splits_and_cash_dividends():
    actions = FakeActions()
    fetch_daily(good_client(["A"]), actions, ["A"], NOV5)
    assert actions.requests
    assert {t for r in actions.requests for t in r.types} == {
        CorporateActionsType.FORWARD_SPLIT, CorporateActionsType.REVERSE_SPLIT, CorporateActionsType.CASH_DIVIDEND}
    assert all(r.start == HISTORY_START and r.end == NOV5 for r in actions.requests)
    # alpaca-py's default limit of 1000 would stop paging early and drop later splits and dividends
    assert all(r.limit is None for r in actions.requests)


def test_fetch_daily_applies_reverse_split_and_cash_dividend():
    client = FakeClient({"A": [fake_day("2020-01-02", 2.0), fake_day("2020-01-03", 2.0), fake_day("2020-01-06", 20.0)]})
    actions = FakeActions(reverse_splits=[("A", date(2020, 1, 6), 10.0, 1.0)],
                          dividends=[("A", date(2020, 1, 3), 0.2)])
    f = [x for b in fetch_daily(client, actions, ["A"], NOV5)["A"] for x in (b.factor_all, b.factor_split)]
    # 1:10 reverse split: split factor 10 before its ex-date; dividend 0.2 on a raw close of 2.0 is 0.9
    assert f == pytest.approx([9.0, 10.0, 10.0, 10.0, 1.0, 1.0])


def test_fetch_daily_batches_at_most_100_symbols():
    symbols = [f"S{i}" for i in range(101)]
    client = good_client(symbols)
    fetch_daily(client, FakeActions(), symbols, NOV5)
    assert sorted(len(r.symbol_or_symbols) for r in client.requests) == [1, 100]


def test_fetch_daily_applies_factors_from_actions():
    client = FakeClient({"A": [fake_day("2020-08-28", 400.0, 4), fake_day("2020-08-31", 100.0, 4)]})
    actions = FakeActions(splits=[("A", date(2020, 8, 31), 1.0, 4.0)])
    bars = fetch_daily(client, actions, ["A"], NOV5)["A"]
    assert [(b.date, b.c, b.factor_all, b.factor_split) for b in bars] == [
        ("2020-08-28", 400.0, 0.25, 0.25), ("2020-08-31", 100.0, 1.0, 1.0)]


def test_split_factor_before_a_4_for_1_split():
    closes = [("2020-08-28", 499.23), ("2020-08-31", 129.04)]
    assert build_factors(closes, [("2020-08-31", 0.25)], []) == [(0.25, 0.25), (1.0, 1.0)]


def test_dividend_uses_the_close_before_the_ex_date():
    # ex-date 2024-01-15 is not a trading day; the close before it is 2024-01-12's
    closes = [("2024-01-11", 99.0), ("2024-01-12", 100.0), ("2024-01-16", 100.0)]
    f = build_factors(closes, [], [("2024-01-15", 1.0)])
    assert [x for pair in f for x in pair] == pytest.approx([0.99, 1.0, 0.99, 1.0, 1.0, 1.0])  # approx can't nest


def test_same_day_dividends_multiply():
    closes = [("2023-02-09", 10.0), ("2023-02-10", 10.0)]
    f = build_factors(closes, [], [("2023-02-10", 0.15), ("2023-02-10", 0.65)])
    assert f[0][0] == pytest.approx((1 - 0.015) * (1 - 0.065))


def test_dividend_as_paid_before_a_split_uses_the_raw_close():
    closes = [("2020-08-06", 400.0), ("2020-08-07", 400.0), ("2020-08-31", 100.0)]
    f = build_factors(closes, [("2020-08-31", 0.25)], [("2020-08-07", 0.82)])
    assert f[0] == pytest.approx((0.25 * (1 - 0.82 / 400), 0.25))
    assert f[1] == pytest.approx((0.25, 0.25))


def test_dividend_on_the_first_bar_needs_no_earlier_close():
    assert build_factors([("2016-01-04", 10.0)], [], [("2016-01-04", 0.1)]) == [(1.0, 1.0)]


@pytest.mark.parametrize("amount", [10.0, 12.0, -1.0, 0.0])
def test_implausible_dividend_is_skipped_with_a_warning(amount, caplog):
    closes = [("2024-01-11", 10.0), ("2024-01-12", 10.0)]
    with caplog.at_level("WARNING", logger="app.daily"):
        assert build_factors(closes, [], [("2024-01-12", amount)]) == [(1.0, 1.0), (1.0, 1.0)]
    assert "2024-01-12" in caplog.text and str(amount) in caplog.text


def test_dividend_after_a_zero_close_is_skipped():
    closes = [("2024-01-11", 0.0), ("2024-01-12", 10.0)]
    assert build_factors(closes, [], [("2024-01-12", 0.1)]) == [(1.0, 1.0), (1.0, 1.0)]


def test_fetch_daily_uses_new_york_date():
    # timestamp 2020-01-02 05:00 UTC is midnight New York on 2020-01-02
    assert fetch_daily(good_client(["A"]), FakeActions(), ["A"], NOV5)["A"][0].date == "2020-01-02"


def test_refresh_is_skipped_when_current(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    store.set_daily_through("2026-11-05")
    client, actions = good_client(["A"]), FakeActions()
    assert refresh_daily(client, actions, store, ["A"], now=NOW) is False
    assert client.requests == [] and actions.requests == []


def test_refresh_sets_marker_only_when_finished(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    assert refresh_daily(good_client(["A"]), FakeActions(), store, ["A"], now=NOW) is True
    assert store.daily_through() == "2026-11-05"
    assert {r[0] for r in store.daily_rows("2099-01-01")} == {"A"}


def test_refresh_reports_progress(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    symbols = [f"S{i:03}" for i in range(101)]
    seen = []
    refresh_daily(good_client(symbols), FakeActions(), store, symbols, now=NOW,
                  progress=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 2), (2, 2)]


def test_interrupted_refresh_leaves_marker_unset_and_symbols_consistent(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    symbols = [f"S{i:03}" for i in range(101)]  # 2 batches: 100, then 1
    store.replace_daily("S100", [row("2019-01-02", "S100", c=1.0)])
    client = good_client(symbols)
    calls = []
    real = client.get_stock_bars

    def flaky(req):
        calls.append(req)
        if len(calls) == 2:
            raise HTTPError(500)
        return real(req)

    client.get_stock_bars = flaky
    with pytest.raises(HTTPError):
        refresh_daily(client, FakeActions(), store, symbols, now=NOW)
    assert store.daily_through() is None
    rows = list(store.daily_rows("2099-01-01"))
    assert {r[0] for r in rows if r[0] != "S100"} == set(symbols[:100])  # first batch written
    assert [r[1] for r in rows if r[0] == "S100"] == ["2019-01-02"]  # second batch's old rows untouched
    assert refresh_daily(good_client(symbols), FakeActions(), store, symbols, now=NOW) is True
    assert store.daily_through() == "2026-11-05"
    assert [r[1] for r in store.daily_rows("2099-01-01") if r[0] == "S100"] == ["2020-01-02"]


def test_failed_actions_request_keeps_old_rows_and_marker_unset(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    old = row("2019-01-02")
    store.replace_daily("A", [old])
    with pytest.raises(HTTPError):
        refresh_daily(good_client(["A"]), FakeActions(error=HTTPError(500)), store, ["A"], now=NOW)
    assert list(store.daily_rows("2099-01-01")) == [("A", "2019-01-02", 10.0, 10.0, 10.0, 10.0, 100.0, 1.0, 1.0)]
    assert store.daily_through() is None


def test_rate_limited_requests_are_retried(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    client = good_client(["A"])
    client.errors = [HTTPError(429, {"Retry-After": "1"})]
    waits = []
    assert refresh_daily(client, FakeActions(), store, ["A"], now=NOW, sleep=waits.append) is True
    assert waits == [1.0] and len(client.requests) == 2


def test_symbol_without_bars_keeps_old_rows_and_finishes(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    store.replace_daily("GONE", [row("2019-01-02", "GONE")])
    assert refresh_daily(good_client(["A"]), FakeActions(), store, ["A", "GONE"], now=NOW) is True
    assert "GONE" in {r[0] for r in store.daily_rows("2099-01-01")}
    assert store.daily_through() == "2026-11-05"


def test_symbol_with_an_empty_bar_list_keeps_old_rows_and_is_logged(tmp_path, caplog):
    store = Store(str(tmp_path / "bars.db"))
    store.replace_daily("GONE", [row("2019-01-02", "GONE")])
    client = FakeClient({"A": [fake_day("2020-01-02")], "GONE": []})  # BarSet can keep a key with no bars
    with caplog.at_level("INFO", logger="app.daily"):
        assert refresh_daily(client, FakeActions(), store, ["A", "GONE"], now=NOW) is True
    assert [r[1] for r in store.daily_rows("2099-01-01") if r[0] == "GONE"] == ["2019-01-02"]
    assert "GONE" in caplog.text


def test_symbol_without_bars_is_logged_as_a_warning(tmp_path, caplog):
    store = Store(str(tmp_path / "bars.db"))
    with caplog.at_level("INFO", logger="app.daily"):
        assert refresh_daily(good_client(["A"]), FakeActions(), store, ["A", "GONE"], now=NOW) is True
    assert [(r.levelname, "GONE" in r.getMessage()) for r in caplog.records] == [("WARNING", True)]


def test_missing_spy_raises_without_the_marker_and_keeps_old_spy_rows(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    store.replace_daily("SPY", [row("2019-01-02", "SPY")])
    with pytest.raises(IncompleteDownload, match="SPY"):
        refresh_daily(good_client(["A"]), FakeActions(), store, ["A", "SPY"], now=NOW)
    assert store.daily_through() is None
    rows = list(store.daily_rows("2099-01-01"))
    assert [r[1] for r in rows if r[0] == "SPY"] == ["2019-01-02"]  # old rows kept
    assert [r[1] for r in rows if r[0] == "A"] == ["2020-01-02"]  # the others were still written
    assert refresh_daily(good_client(["A", "SPY"]), FakeActions(), store, ["A", "SPY"], now=NOW) is True
    assert store.daily_through() == "2026-11-05"
    assert [r[1] for r in store.daily_rows("2099-01-01") if r[0] == "SPY"] == ["2020-01-02"]


def test_spy_with_an_empty_bar_list_also_raises(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    client = FakeClient({"A": [fake_day("2020-01-02")], "SPY": []})
    with pytest.raises(IncompleteDownload, match="SPY"):
        refresh_daily(client, FakeActions(), store, ["A", "SPY"], now=NOW)
    assert store.daily_through() is None


def test_load_bars_hides_a_day_before_its_cutoff(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    store.replace_daily("A", [row("2026-11-04"), row("2026-11-05")])
    before = load_bars(store, now=datetime(2026, 11, 5, 20, 29, tzinfo=ET))
    after = load_bars(store, now=datetime(2026, 11, 5, 20, 30, tzinfo=ET))
    assert before["date"].max() == pd.Timestamp("2026-11-04")
    assert after["date"].max() == pd.Timestamp("2026-11-05")
    assert list(after.columns) == ["symbol", "date", "o", "h", "l", "c", "v", "factor_all", "factor_split"]
    assert str(after["date"].dtype).startswith("datetime64")


def test_load_bars_empty_store(tmp_path):
    bars = load_bars(Store(str(tmp_path / "bars.db")), now=NOW)
    assert bars.empty and str(bars["date"].dtype).startswith("datetime64")


def test_universe_adds_spy():
    assert universe()[-1] == "SPY" and "AAPL" in universe()
