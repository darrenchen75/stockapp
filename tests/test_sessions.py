import logging
import threading
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from app import feed, market, sessions
from app.sessions import Session
from app.store import ET
from tests.test_feed import FakeClient, HTTPError, fake_bar
from tests.test_store import bar, store, ts

EARLY = Session(date(2026, 11, 27), ts(2026, 11, 27, 9, 30), ts(2026, 11, 27, 13, 0))


def test_regular_excludes_closing_minute_and_extended_hours():
    day = Session(date(2026, 10, 16), ts(2026, 10, 16, 9, 30), ts(2026, 10, 16, 16, 0))
    bars = [bar("A", ts(2026, 10, 16, h, m)) for h, m in [(8, 0), (9, 30), (15, 59), (16, 0), (19, 0)]]
    assert [b.ts for b in sessions.regular(bars, [day])[day.day]] == [ts(2026, 10, 16, 9, 30), ts(2026, 10, 16, 15, 59)]


def test_regular_respects_early_close():
    bars = [bar("A", ts(2026, 11, 27, 12, 59)), bar("A", ts(2026, 11, 27, 13, 0)), bar("A", ts(2026, 11, 27, 15, 0))]
    assert [b.ts for b in sessions.regular(bars, [EARLY])[EARLY.day]] == [ts(2026, 11, 27, 12, 59)]


def test_x_mapping_skips_weekends_and_holidays_and_round_trips():
    hol = [date(2026, 11, 26)]  # Thanksgiving
    wed, fri = ts(2026, 11, 25, 19, 59), ts(2026, 11, 27, 4, 0)
    assert sessions.to_x(fri, hol) - sessions.to_x(wed, hol) == 1
    assert sessions.to_x(ts(2026, 11, 28, 10, 0), hol) is None
    assert sessions.to_x(ts(2026, 11, 25, 20, 0), hol) is None
    assert sessions.from_x(sessions.to_x(wed, hol), hol) == wed


def test_holidays_are_missing_weekdays():
    s = [Session(date(2026, 11, d), 0, 1) for d in (25, 27)]
    assert sessions.holidays(s) == [date(2026, 11, 26)]


def test_market_refresh_failure_is_logged_not_raised(tmp_path, caplog):
    class Rejecting:
        def get_calendar(self, req): raise HTTPError(401)
        def get_all_assets(self, req): return _assets(A="NYSE")
    market.refresh(Rejecting(), store(tmp_path), datetime(2026, 11, 16, 12, tzinfo=ET))
    assert store(tmp_path).sessions() == [] and "calendar" in caplog.text


def test_refresh_saves_and_skips_when_fresh(tmp_path):
    calls = []

    class Trading:
        def get_all_assets(self, req): return _assets(A="NYSE")

        def get_calendar(self, req):
            calls.append(req)
            return [SimpleNamespace(date=date(2026, 11, 16), open=datetime(2026, 11, 16, 9, 30),
                                    close=datetime(2026, 11, 16, 16, 0))]
    s, now = store(tmp_path), datetime(2026, 11, 16, 12, tzinfo=ET)
    market.refresh(Trading(), s, now)
    market.refresh(Trading(), s, now)
    assert len(calls) == 1
    assert s.sessions() == [Session(date(2026, 11, 16), ts(2026, 11, 16, 9, 30), ts(2026, 11, 16, 16, 0))]
    assert (s.meta("calendar_from"), s.meta("calendar_to")) == ("2026-10-12", "2026-11-26")


def test_expired_calendar_falls_back_for_uncovered_bar_dates(tmp_path):
    s = store(tmp_path)
    s.save_sessions([Session(date(2026, 11, 2), ts(2026, 11, 2, 9, 30), ts(2026, 11, 2, 16, 0))])
    s.set_meta("calendar_from", "2026-11-02"); s.set_meta("calendar_to", "2026-11-03")
    s.upsert([bar("A", ts(2026, 11, 3, 10, 0)), bar("A", ts(2026, 11, 16, 10, 0))])
    days = [x.day for x in sessions.load(s)]
    assert date(2026, 11, 16) in days and date(2026, 11, 3) not in days


def test_poller_calendar_401_does_not_report_bad_key(tmp_path):
    class Rejecting:
        def get_calendar(self, req): raise HTTPError(401)
        def get_all_assets(self, req): return _assets(A="NYSE")
    t = datetime.now(timezone.utc)
    client = FakeClient({"A": [fake_bar(t.replace(microsecond=0))]})
    auth, updated = [], threading.Event()
    p = feed.Poller(client, str(tmp_path / "b.db"), ["A"], trading=Rejecting(), poll_seconds=60,
                    on_auth_failed=lambda: auth.append(1), on_updated=updated.set)
    p.start()
    assert updated.wait(10)
    p.halt()
    assert auth == []


def test_refresh_failure_retries_at_most_hourly(tmp_path):
    calls = []

    class Rejecting:
        def get_calendar(self, req): calls.append(1); raise HTTPError(401)
        def get_all_assets(self, req): return _assets(A="NYSE")
    s, now = store(tmp_path), datetime(2026, 11, 16, 12, tzinfo=ET)
    market.refresh(Rejecting(), s, now)
    market.refresh(Rejecting(), s, now + timedelta(minutes=59))
    assert len(calls) == 1
    market.refresh(Rejecting(), s, now + timedelta(minutes=61))
    assert len(calls) == 2


def test_mapping_30k_bars_is_fast():
    import time
    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(30) if (date(2026, 10, 1) + timedelta(days=i)).weekday() < 5]
    sess = [Session(d, _t(d, 9, 30), _t(d, 16, 0)) for d in days]
    bars = [bar("A", _t(d, 9, 30) + 60 * i) for d in days for i in range(1430)][:30000]
    hol = sessions.holidays(sess)
    t0 = time.perf_counter()
    for b in bars:
        sessions.to_x(b.ts, hol)
    sessions.regular(bars, sess)
    assert time.perf_counter() - t0 < 1


def _t(d, h, m):
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=ET).timestamp())


def _assets(**kw):
    return [SimpleNamespace(symbol=k, exchange=SimpleNamespace(value=v)) for k, v in kw.items()]


def test_refresh_caches_exchanges_for_a_week(tmp_path):
    calls = []

    class Trading:
        def get_calendar(self, req): return []
        def get_all_assets(self, req):
            calls.append(req)
            return _assets(AAPL="NASDAQ", KO="NYSE")
    s, now = store(tmp_path), datetime(2026, 11, 16, 12, tzinfo=ET)
    market.refresh(Trading(), s, now)
    market.refresh(Trading(), s, now + timedelta(days=6))
    assert len(calls) == 1 and s.exchanges() == {"AAPL": "NASDAQ", "KO": "NYSE"}
    assert s.meta("exchanges_at") == now.isoformat()
    market.refresh(Trading(), s, now + timedelta(days=8))
    assert len(calls) == 2


def test_exchange_failure_is_logged_retried_hourly_and_spares_calendar(tmp_path, caplog):
    calls = []

    class Trading:
        def get_calendar(self, req):
            return [SimpleNamespace(date=date(2026, 11, 16), open=datetime(2026, 11, 16, 9, 30),
                                    close=datetime(2026, 11, 16, 16, 0))]
        def get_all_assets(self, req): calls.append(1); raise HTTPError(401)
    s, now = store(tmp_path), datetime(2026, 11, 16, 12, tzinfo=ET)
    market.refresh(Trading(), s, now)
    market.refresh(Trading(), s, now + timedelta(minutes=59))
    assert len(calls) == 1 and s.exchanges() == {} and "exchanges" in caplog.text
    assert len(s.sessions()) == 1
    market.refresh(Trading(), s, now + timedelta(minutes=61))
    assert len(calls) == 2


def test_calendar_failure_spares_exchanges(tmp_path):
    class Trading:
        def get_calendar(self, req): raise HTTPError(500)
        def get_all_assets(self, req): return _assets(AAPL="NASDAQ")
    s = store(tmp_path)
    market.refresh(Trading(), s, datetime(2026, 11, 16, 12, tzinfo=ET))
    assert s.exchanges() == {"AAPL": "NASDAQ"}


def test_empty_exchange_list_keeps_old_rows_and_retries(tmp_path, caplog):
    class Trading:
        def get_calendar(self, req): return []
        def get_all_assets(self, req): return []
    s, now = store(tmp_path), datetime(2026, 11, 16, 12, tzinfo=ET)
    s.save_exchanges({"AAPL": "NASDAQ"})
    market.refresh(Trading(), s, now)
    assert s.exchanges() == {"AAPL": "NASDAQ"} and s.meta("exchanges_at") is None
    assert s.meta("exchanges_failed_at") and "exchanges" in caplog.text
