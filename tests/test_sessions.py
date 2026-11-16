import logging
import threading
from datetime import date, datetime, timezone
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
    market.refresh(Rejecting(), store(tmp_path), datetime(2026, 11, 16, 12, tzinfo=ET))
    assert store(tmp_path).sessions() == [] and "calendar" in caplog.text


def test_refresh_saves_and_skips_when_fresh(tmp_path):
    calls = []

    class Trading:
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
    t = datetime.now(timezone.utc)
    client = FakeClient({"A": [fake_bar(t.replace(microsecond=0))]})
    auth, updated = [], threading.Event()
    p = feed.Poller(client, str(tmp_path / "b.db"), ["A"], trading=Rejecting(), poll_seconds=60,
                    on_auth_failed=lambda: auth.append(1), on_updated=updated.set)
    p.start()
    assert updated.wait(10)
    p.halt()
    assert auth == []
