import sqlite3
from datetime import datetime

import pytest

from app.store import ET, Bar, DailyBar, Store


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


def test_summary_reads_one_snapshot_while_history_is_replaced(tmp_path):
    # The poller's split repair commits on its own connection between summary()'s queries.
    s, writer = store(tmp_path), store(tmp_path)
    s.upsert([bar("AAPL", ts(2026, 10, 16, 15, 59), c=100.0)])
    latest_all = s.latest_all

    def racing():
        out = latest_all()
        writer.replace("AAPL", [bar("AAPL", ts(2026, 10, 16, 15, 58), c=50.0)], None)
        return out

    s.latest_all = racing
    assert s.summary()["AAPL"].last == 100.0  # pre-repair snapshot, not a crash on the vanished minute
    s.latest_all = latest_all
    assert s.summary()["AAPL"].last == 50.0


def test_replace_swaps_all_rows_and_records_split(tmp_path):
    s = store(tmp_path)
    s.upsert([bar("AAPL", 60, c=100.0), bar("AAPL", 120, c=100.0), bar("MSFT", 60)])
    s.replace("AAPL", [bar("AAPL", 120, c=50.0)], "2026-10-20")
    assert s.bars("AAPL", 0) == [bar("AAPL", 120, c=50.0)]  # minute missing from the refetch is gone, not left raw
    assert s.bars("MSFT", 0) == [bar("MSFT", 60)]
    assert s.handled_splits() == {("AAPL", "2026-10-20")}


def test_gaps_counts_trading_days_missing_per_symbol(tmp_path):
    s = store(tmp_path)
    days = [19, 20, 21, 22, 23]
    s.upsert([bar("AAPL", ts(2026, 10, d, 10, 0)) for d in days])
    s.upsert([bar("MSFT", ts(2026, 10, d, 10, 0)) for d in (20, 22)])  # 19 is the oldest day: skipped
    s.upsert([bar("NVDA", ts(2026, 10, 19, 10, 0))])
    now = ts(2026, 10, 23, 16, 0)  # today (23) is still in progress: skipped
    assert s.gaps(["AAPL", "MSFT", "NVDA", "GONE"], now=now) == {"AAPL": 0, "MSFT": 1, "NVDA": 3}


def daily(symbol, date, c=10.0, f=1.0):
    return DailyBar(symbol, date, c, c, c, c, 100.0, f, f)


def test_replace_daily_swaps_all_rows_of_one_symbol(tmp_path):
    s = store(tmp_path)
    s.replace_daily("A", [daily("A", "2020-01-02"), daily("A", "2020-01-03")])
    s.replace_daily("B", [daily("B", "2020-01-02")])
    s.replace_daily("A", [daily("A", "2020-01-03", f=0.5)])
    rows = list(s.daily_rows("2099-01-01"))
    assert [(r[0], r[1], r[7]) for r in rows] == [("B", "2020-01-02", 1.0), ("A", "2020-01-03", 0.5)]


def test_daily_rows_stop_at_through(tmp_path):
    s = store(tmp_path)
    s.replace_daily("A", [daily("A", "2020-01-02"), daily("A", "2020-01-03")])
    assert [r[1] for r in s.daily_rows("2020-01-02")] == ["2020-01-02"]


def test_failed_replace_keeps_old_rows(tmp_path):
    s = store(tmp_path)
    s.replace_daily("A", [daily("A", "2020-01-02")])
    with pytest.raises(sqlite3.Error):
        s.replace_daily("A", [daily("A", "2020-01-03"), daily("A", "2020-01-03")])
    assert [r[1] for r in s.daily_rows("2099-01-01")] == ["2020-01-02"]


def test_prune_leaves_daily_rows(tmp_path):
    s = store(tmp_path)
    s.replace_daily("A", [daily("A", "2016-01-04")])
    s.prune(30)
    assert len(list(s.daily_rows("2099-01-01"))) == 1


def test_daily_through_round_trips(tmp_path):
    s = store(tmp_path)
    assert s.daily_through() is None
    s.set_daily_through("2026-11-05")
    assert s.daily_through() == "2026-11-05"


def test_daily_write_while_another_connection_reads(tmp_path):
    a, b = store(tmp_path), store(tmp_path)
    a.replace_daily("A", [daily("A", "2020-01-02")])
    b.db.execute("BEGIN")
    b.db.execute("SELECT * FROM bars").fetchall()
    a.replace_daily("A", [daily("A", "2020-01-03")])  # must not raise "database is locked"
    b.db.commit()
