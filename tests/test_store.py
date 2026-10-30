from datetime import datetime

from app.store import ET, Bar, Store


def ts(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


def bar(symbol, t, c=10.0, v=100):
    return Bar(symbol, t, c, c, c, c, v)


def store(tmp_path):
    return Store(str(tmp_path / "bars.db"))


def test_upsert_keeps_one_row_per_symbol_minute(tmp_path):
    s = store(tmp_path)
    t = ts(2026, 10, 19, 9, 30)
    s.upsert([bar("AAPL", t, c=1.0)])
    s.upsert([bar("AAPL", t, c=2.0)])
    assert s.bars("AAPL", 0) == [bar("AAPL", t, c=2.0)]


def test_data_survives_reopen(tmp_path):
    store(tmp_path).upsert([bar("AAPL", 60)])
    assert store(tmp_path).bars("AAPL", 0) == [bar("AAPL", 60)]


def test_latest_all_and_latest(tmp_path):
    s = store(tmp_path)
    assert s.latest() is None
    s.upsert([bar("AAPL", 60), bar("AAPL", 120), bar("MSFT", 60)])
    assert s.latest_all() == {"AAPL": 120, "MSFT": 60}
    assert s.latest() == 120


def test_bars_since_is_inclusive_and_ordered(tmp_path):
    s = store(tmp_path)
    s.upsert([bar("AAPL", 180), bar("AAPL", 60), bar("AAPL", 120), bar("MSFT", 120)])
    assert [b.ts for b in s.bars("AAPL", 120)] == [120, 180]


def test_prune_drops_rows_older_than_30_days(tmp_path):
    s = store(tmp_path)
    now = 100 * 86400
    s.upsert([bar("AAPL", now - 31 * 86400), bar("AAPL", now - 29 * 86400)])
    assert s.prune(days=30, now=now) == 1
    assert [b.ts for b in s.bars("AAPL", 0)] == [now - 29 * 86400]


def test_summary_empty_store(tmp_path):
    s = store(tmp_path)
    assert s.session_start() is None
    assert s.summary() == {}


def test_summary_compares_last_session_to_previous_close(tmp_path):
    # Thursday + Friday bars; app opened on the weekend.
    s = store(tmp_path)
    s.upsert([
        bar("AAPL", ts(2026, 10, 15, 15, 59), c=100.0, v=5),
        bar("AAPL", ts(2026, 10, 16, 9, 30), c=101.0, v=10),
        bar("AAPL", ts(2026, 10, 16, 15, 59), c=102.0, v=20),
    ])
    x = s.summary()["AAPL"]
    assert (x.last, x.prev_close, x.volume) == (102.0, 100.0, 30)
    assert round(x.pct, 2) == 2.0


def test_session_uses_new_york_date_not_utc(tmp_path):
    # 19:30 EST on Nov 20 is 00:30 UTC on Nov 21; it still belongs to Nov 20's session.
    s = store(tmp_path)
    s.upsert([
        bar("AAPL", ts(2026, 11, 20, 9, 30), c=50.0, v=1),
        bar("AAPL", ts(2026, 11, 20, 19, 30), c=51.0, v=2),
    ])
    assert s.session_start() == ts(2026, 11, 20, 0, 0)
    x = s.summary()["AAPL"]
    assert (x.last, x.prev_close, x.volume, x.pct) == (51.0, None, 3, None)


def test_summary_is_per_symbol_when_one_is_stale(tmp_path):
    # MSFT has no bars in AAPL's latest session (halt, delisting, partial response):
    # it keeps its own last-session change instead of a fake 0%.
    s = store(tmp_path)
    s.upsert([
        bar("AAPL", ts(2026, 10, 15, 15, 59), c=100.0, v=5),
        bar("AAPL", ts(2026, 10, 16, 15, 59), c=102.0, v=20),
        bar("MSFT", ts(2026, 10, 14, 15, 59), c=200.0, v=7),
        bar("MSFT", ts(2026, 10, 15, 15, 59), c=210.0, v=9),
    ])
    a, m = s.summary()["AAPL"], s.summary()["MSFT"]
    assert (a.last, a.prev_close, a.volume) == (102.0, 100.0, 20)
    assert (m.last, m.prev_close, m.volume) == (210.0, 200.0, 9)
    assert s.session_start() == ts(2026, 10, 16, 0, 0)
    assert s.session_start("MSFT") == ts(2026, 10, 15, 0, 0)
