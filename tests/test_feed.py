import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import feed
from app.store import Bar, Store

NOW = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)
NOW_TS = int(NOW.timestamp())
FLOOR = NOW - timedelta(days=30)
KEEP_FLOOR = NOW - timedelta(days=35)
END = NOW - timedelta(minutes=16)  # 15-minute SIP delay plus a minute for clock skew
SIP_403 = "subscription does not permit querying recent SIP data"


def fake_bar(t: datetime, c=10.0):
    return SimpleNamespace(timestamp=t, open=c, high=c, low=c, close=c, volume=100.0)


class HTTPError(Exception):
    def __init__(self, status, headers=None, message=None):
        super().__init__(message or f"HTTP {status}")
        self.status_code = status
        self.response = SimpleNamespace(headers=headers or {})


class FakeClient:
    def __init__(self, data=None, errors=()):
        self.data = data or {}  # symbol -> list of fake bars
        self.errors = list(errors)
        self.requests = []

    def get_stock_bars(self, req):
        self.requests.append(req)
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(
            data={s: self.data[s] for s in req.symbol_or_symbols if s in self.data}
        )


class StrictClient(FakeClient):
    """Rejects inverted windows and ends newer than 15 minutes ago (the free SIP plan's limit)."""
    def __init__(self, data=None, errors=(), now=None):
        super().__init__(data, errors)
        self.now = now  # None = the real clock, for the poller

    def get_stock_bars(self, req):
        now = self.now or datetime.now(timezone.utc)
        if utc(req.start) >= utc(req.end) or utc(req.end) > now - timedelta(minutes=15):
            raise AssertionError(f"bad window {req.start}..{req.end}")
        return super().get_stock_bars(req)


class FailSecond(FakeClient):
    def get_stock_bars(self, req):
        if self.requests:
            self.requests.append(req)
            raise HTTPError(500)
        return super().get_stock_bars(req)


def utc(dt: datetime) -> datetime:
    # alpaca-py may normalize tz-aware datetimes to naive UTC; compare either way.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def syms(n, prefix="S"):
    return [f"{prefix}{i:03}" for i in range(n)]


def test_load_symbols():
    symbols = feed.load_symbols()
    assert 490 <= len(symbols) <= 510
    assert "AAPL" in symbols and "BRK.B" in symbols
    assert len(set(symbols)) == len(symbols)
    assert all(s.strip() == s and s for s in symbols)


def test_plan_first_run_fetches_35_days_in_batches_of_8():
    jobs = feed.plan(syms(25), {}, NOW, include_empty=True)
    assert [len(b) for b, _ in jobs] == [8, 8, 8, 1]
    assert all(start == KEEP_FLOOR for _, start in jobs)


def test_plan_poll_batches_known_symbols_by_100():
    latest = {s: NOW_TS - 120 for s in syms(250)}
    jobs = feed.plan(list(latest), latest, NOW, include_empty=False)
    assert [len(b) for b, _ in jobs] == [100, 100, 50]
    assert all(start == NOW - timedelta(seconds=120) for _, start in jobs)


def test_plan_poll_skips_symbols_with_no_data():
    jobs = feed.plan(["AAPL", "GONE"], {"AAPL": NOW_TS - 60}, NOW, include_empty=False)
    assert [b for b, _ in jobs] == [["AAPL"]]


def test_plan_never_starts_before_35_day_floor():
    jobs = feed.plan(["AAPL"], {"AAPL": NOW_TS - 40 * 86400}, NOW, include_empty=False)
    assert jobs == [(["AAPL"], KEEP_FLOOR)]


def test_plan_drops_symbols_already_at_the_request_end():
    # A bar newer than the end (an IEX bar from before the delay) must not make an inverted window.
    latest = {"NEW": NOW_TS + 600, "AT": NOW_TS, "OLD": NOW_TS - 60}
    assert feed.plan(list(latest), latest, NOW, include_empty=False) == [(["OLD"], NOW - timedelta(seconds=60))]


def test_plan_bounds_request_size():
    # Back after 20 days: every symbol is stale; no request may exceed MAX_SPAN symbol-minutes.
    latest = {s: NOW_TS - 20 * 86400 for s in syms(500)}
    latest.update({s: NOW_TS - 60 for s in syms(5, prefix="R")})
    jobs = feed.plan(list(latest), latest, NOW, include_empty=True)
    assert sorted(s for b, _ in jobs for s in b) == sorted(latest)
    for batch, start in jobs:
        minutes = (NOW - start).total_seconds() // 60
        assert len(batch) == 1 or len(batch) * minutes <= feed.MAX_SPAN


def test_fetch_converts_bars():
    t = NOW - timedelta(minutes=1)
    client = FakeClient({"AAPL": [fake_bar(t, c=5.0)]})
    bars = feed.fetch(client, ["AAPL", "MSFT"], FLOOR, NOW)
    assert [(b.symbol, b.ts, b.c, b.v) for b in bars] == [("AAPL", int(t.timestamp()), 5.0, 100)]
    req = client.requests[0]
    assert req.symbol_or_symbols == ["AAPL", "MSFT"]
    assert utc(req.start) == FLOOR


def test_fetch_retries_after_rate_limit():
    client = FakeClient({"AAPL": [fake_bar(NOW)]}, errors=[HTTPError(429), HTTPError(429)])
    sleeps = []
    assert len(feed.fetch(client, ["AAPL"], FLOOR, NOW, sleep=sleeps.append)) == 1
    assert sleeps == [5, 10]


@pytest.mark.parametrize("headers, wait", [
    ({"Retry-After": "42"}, 42),
    ({"Retry-After": "999"}, feed.MAX_WAIT),
    ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, 5),  # HTTP-date form: fall back
])
def test_fetch_honors_retry_after(headers, wait):
    client = FakeClient({"AAPL": [fake_bar(NOW)]}, errors=[HTTPError(429, headers)])
    sleeps = []
    feed.fetch(client, ["AAPL"], FLOOR, NOW, sleep=sleeps.append)
    assert sleeps == [wait]


def test_fetch_waits_for_rate_limit_reset():
    reset = time.time() + 30
    client = FakeClient({"AAPL": [fake_bar(NOW)]},
                        errors=[HTTPError(429, {"X-RateLimit-Reset": str(int(reset))})])
    sleeps = []
    feed.fetch(client, ["AAPL"], FLOOR, NOW, sleep=sleeps.append)
    assert 28 <= sleeps[0] <= 30


def test_fetch_gives_up_after_retries():
    client = FakeClient(errors=[HTTPError(429)] * feed.RETRIES)
    with pytest.raises(HTTPError):
        feed.fetch(client, ["AAPL"], FLOOR, NOW, sleep=lambda s: None)


def test_fetch_does_not_retry_auth_errors():
    client = FakeClient(errors=[HTTPError(401)])
    sleeps = []
    with pytest.raises(HTTPError):
        feed.fetch(client, ["AAPL"], FLOOR, NOW, sleep=sleeps.append)
    assert sleeps == []


def test_fetch_asks_for_sip_ending_15_minutes_ago(tmp_path):
    c = FakeClient()
    feed.backfill(c, Store(str(tmp_path / "bars.db")), ["A"], now=NOW)
    assert c.requests and all(r.feed == feed.DataFeed.SIP and utc(r.end) <= END for r in c.requests)


def test_backfill_resumes_from_last_bar(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    t1, t2 = NOW - timedelta(minutes=20), NOW - timedelta(minutes=17)
    client = FakeClient({"AAPL": [fake_bar(t1), fake_bar(t2)]})
    progress = []
    n = feed.backfill(client, store, ["AAPL", "GONE"], now=NOW, progress=lambda d, t: progress.append((d, t)))
    assert n == 2 and progress == [(1, 1)]
    assert store.latest_all() == {"AAPL": int(t2.timestamp())}

    feed.backfill(client, store, ["AAPL", "GONE"], include_empty=False, now=NOW)
    resumed = client.requests[-1]
    assert resumed.symbol_or_symbols == ["AAPL"]
    assert utc(resumed.start) == t2


def sip(db):
    Store(db).set_meta("feed", "sip")
    return db


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("migrated", [False, True])  # a data-client 401/403 is a real key failure in both phases
def test_poller_stops_on_rejected_key(tmp_path, migrated, status):
    seen = []
    db = str(tmp_path / "bars.db")
    if migrated:
        sip(db)
    p = feed.Poller(FakeClient(errors=[HTTPError(status)]), db, ["AAPL"],
                    on_auth_failed=lambda: seen.append("auth"))
    p.run()  # must return instead of looping
    assert seen == ["auth"]


def test_requests_end_a_minute_before_the_sip_delay():
    assert feed.DELAY == timedelta(minutes=15) and feed.MARGIN == timedelta(minutes=1)


@pytest.mark.parametrize("migrated", [False, True])
def test_poller_treats_recent_sip_403_as_offline(tmp_path, migrated):
    # The local clock running ahead of Alpaca's makes the end too recent: retry, don't ask for keys.
    db = str(tmp_path / "bars.db")
    if migrated:
        sip(db)
    client = FakeClient(errors=[HTTPError(403, message=SIP_403)])
    statuses, seen = [], []
    p = feed.Poller(client, db, ["AAPL"], poll_seconds=0, retry_seconds=0,
                    on_status=statuses.append, on_auth_failed=lambda: seen.append("auth"))
    p.on_updated = p.stop.set
    p.run()
    assert seen == [] and statuses == ["Offline — retrying in 0s"] and len(client.requests) == 2


def test_poller_reports_offline_and_retries(tmp_path):
    client = FakeClient(errors=[ConnectionError("down")])
    statuses = []
    p = feed.Poller(client, sip(str(tmp_path / "bars.db")), ["AAPL"], poll_seconds=0, retry_seconds=0,
                    on_status=statuses.append)
    p.on_updated = p.stop.set  # stop after the first successful poll
    p.run()
    assert statuses == ["Offline — retrying in 0s"]
    assert len(client.requests) == 2


def test_poller_prunes_to_35_days_while_running(tmp_path):
    db = sip(str(tmp_path / "bars.db"))
    kept = int(time.time()) - 34 * 86400
    Store(db).upsert([Bar("AAPL", int(time.time()) - 36 * 86400, 1.0, 1.0, 1.0, 1.0, 1),
                      Bar("AAPL", kept, 1.0, 1.0, 1.0, 1.0, 1)])
    p = feed.Poller(FakeClient(), db, ["AAPL"], poll_seconds=0)
    p.on_updated = p.stop.set
    p.run()
    assert [b.ts for b in Store(db).bars("AAPL", 0)] == [kept]


class FakeActions:
    def __init__(self, splits):
        self.splits = splits  # (symbol, ex_date) pairs
        self.requests = []

    def get_corporate_actions(self, req):
        self.requests.append(req)
        return SimpleNamespace(data={"forward_splits": [
            SimpleNamespace(symbol=s, ex_date=d) for s, d in self.splits
        ]})


def test_fetch_requests_split_adjusted_bars():
    client = FakeClient()
    feed.fetch(client, ["AAPL"], FLOOR, NOW)
    assert client.requests[0].adjustment == feed.Adjustment.SPLIT
    assert client.requests[0].feed == feed.DataFeed.SIP


def test_repair_splits_refetches_once(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    t = NOW - timedelta(minutes=1)
    store.upsert([Bar("AAPL", int(t.timestamp()), 100, 100, 100, 100, 1)])
    client = FakeClient({"AAPL": [fake_bar(t, c=50.0)]})
    actions = FakeActions([("AAPL", NOW.date() - timedelta(days=1))])
    assert feed.repair_splits(client, actions, store, ["AAPL"], now=NOW) == 0  # nothing pending
    assert store.bars("AAPL", 0)[0].c == 50.0
    assert utc(client.requests[0].start) == KEEP_FLOOR and utc(client.requests[0].end) == END

    assert feed.repair_splits(client, actions, store, ["AAPL"], now=NOW) == 0
    assert len(client.requests) == 1  # already handled: no refetch


def test_repair_splits_keeps_cache_on_empty_refetch(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    store.upsert([Bar("AAPL", NOW_TS - 60, 100, 100, 100, 100, 1)])
    actions = FakeActions([("AAPL", NOW.date())])
    assert feed.repair_splits(FakeClient(), actions, store, ["AAPL"], now=NOW) == 1
    assert store.bars("AAPL", 0)[0].c == 100
    assert store.handled_splits() == set()  # still pending


def test_repair_splits_refetches_again_after_ex_date(tmp_path):
    # On the ex-date Alpaca may not have adjusted history yet: repair now, confirm tomorrow.
    store = Store(str(tmp_path / "bars.db"))
    client = FakeClient({"AAPL": [fake_bar(NOW - timedelta(minutes=1), c=50.0)]})
    actions = FakeActions([("AAPL", NOW.date())])
    assert feed.repair_splits(client, actions, store, ["AAPL"], now=NOW) == 1  # pending: retry hourly today
    assert store.bars("AAPL", 0)[0].c == 50.0
    assert store.handled_splits() == set()
    client.data["AAPL"] = [fake_bar(NOW - timedelta(minutes=1), c=25.0)]  # Alpaca adjusts later that day
    assert feed.repair_splits(client, actions, store, ["AAPL"], now=NOW + timedelta(hours=1)) == 1
    assert store.bars("AAPL", 0)[0].c == 25.0
    feed.repair_splits(client, actions, store, ["AAPL"], now=NOW + timedelta(days=1))
    assert store.handled_splits() == {("AAPL", NOW.date().isoformat())}


def test_poller_survives_failed_split_check(tmp_path):
    class Broken:
        def get_corporate_actions(self, req):
            raise ConnectionError("down")

    p = feed.Poller(FakeClient(), sip(str(tmp_path / "bars.db")), ["AAPL"], actions=Broken(), poll_seconds=0)
    p.on_updated = p.stop.set
    p.run()  # bar polling still reports an update


def test_backfill_stops_between_batches(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    stop = threading.Event()
    client = FakeClient()
    n = feed.backfill(client, store, syms(25), now=NOW, stop=stop,
                      progress=lambda d, t: stop.set())  # 4 batches of 8; stop after the first
    assert n == 0 and len(client.requests) == 1


def test_repair_splits_stops_between_refetches(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    client = FakeClient({"AAPL": [fake_bar(NOW)], "MSFT": [fake_bar(NOW)]})
    day = NOW.date() - timedelta(days=1)
    stop = threading.Event()
    stop.set()
    assert feed.repair_splits(client, FakeActions([("AAPL", day), ("MSFT", day)]), store,
                              ["AAPL", "MSFT"], now=NOW, stop=stop) == 2  # skipped splits stay pending
    assert client.requests == []


def test_poller_halt_ends_during_rate_limit_wait(tmp_path):
    statuses = []
    client = FakeClient(errors=[HTTPError(429, {"Retry-After": "60"})] * 5)
    p = feed.Poller(client, str(tmp_path / "bars.db"), ["AAPL"], on_status=statuses.append)
    p.start()
    while not client.requests:  # let it reach the 429 wait
        time.sleep(0.01)
    t0 = time.time()
    assert p.halt(timeout=5)
    assert time.time() - t0 < 5
    assert statuses == [] and len(client.requests) == 1  # a stop is not an outage, and no retry burst


def test_poller_halt_waits_for_thread(tmp_path):
    p = feed.Poller(FakeClient(), str(tmp_path / "bars.db"), ["AAPL"], poll_seconds=3600)
    p.start()
    assert p.halt(timeout=5) and not p.thread.is_alive()


def test_stopped_poller_does_not_report_update(tmp_path):
    updated = []
    p = feed.Poller(FakeClient(), str(tmp_path / "bars.db"), syms(25), on_updated=lambda: updated.append(1))
    p.on_progress = lambda d, t: p.stop.set()
    p.run()
    assert updated == []


def test_halt_before_start():
    assert feed.Poller(FakeClient(), "x.db", ["AAPL"]).halt()


def test_repair_keeps_35_days_and_finds_older_split(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    ref = NOW - timedelta(days=34)  # the 30D change's reference close
    store.upsert([Bar("AAPL", int(ref.timestamp()), 100, 100, 100, 100, 1)])
    client = StrictClient({"AAPL": [fake_bar(ref, c=50.0), fake_bar(END, c=50.0)]}, now=NOW)
    actions = FakeActions([("AAPL", NOW.date() - timedelta(days=33))])
    assert feed.repair_splits(client, actions, store, ["AAPL"], now=NOW) == 0
    assert actions.requests[0].start == NOW.date() - timedelta(days=35)
    assert utc(client.requests[0].start) == KEEP_FLOOR
    assert [(b.ts, b.c) for b in store.bars("AAPL", 0)] == [(int(ref.timestamp()), 50.0), (int(END.timestamp()), 50.0)]


def iex(store, symbol="A", t=NOW_TS - 3600):
    store.upsert([Bar(symbol, t, 1.0, 1.0, 1.0, 1.0, 1)])


def test_migrate_error_leaves_iex_bars_and_keeps_staged(tmp_path):
    s = Store(str(tmp_path / "bars.db")); iex(s)
    c = FailSecond(data={x: [fake_bar(END)] for x in syms(20)})
    with pytest.raises(HTTPError):
        feed.migrate(c, s, syms(20), now=NOW)
    assert len(c.requests) == 2
    assert s.bars("A", 0)[0].c == 1.0 and s.meta("feed") is None and s.staged_latest_all()
    assert s.latest_all() == {"A": NOW_TS - 3600}


def test_migrate_stop_returns_false_and_resume_finishes(tmp_path):
    s = Store(str(tmp_path / "bars.db")); iex(s)
    c = StrictClient(data={x: [fake_bar(END)] for x in syms(20)}, now=NOW)
    stop = threading.Event()
    assert feed.migrate(c, s, syms(20), now=NOW, stop=stop, progress=lambda d, t: stop.set()) is False
    assert s.latest_all() == {"A": NOW_TS - 3600} and s.meta("feed") is None
    first = {x for r in c.requests for x in r.symbol_or_symbols}
    assert len(first) == 8
    c.requests.clear()
    assert feed.migrate(c, s, syms(20), now=NOW) is True
    assert {x for r in c.requests for x in r.symbol_or_symbols} == set(syms(20)) - first
    assert s.latest_all() == {x: int(END.timestamp()) for x in syms(20)}  # IEX-only "A" is gone
    assert s.meta("feed") == "sip" and s.staged_latest_all() == {}


def test_migrate_with_no_sip_bars_keeps_iex_bars(tmp_path):
    s = Store(str(tmp_path / "bars.db")); iex(s)
    assert feed.migrate(StrictClient(now=NOW), s, syms(20), now=NOW) is False
    assert s.latest_all() == {"A": NOW_TS - 3600} and s.meta("feed") is None


def test_poller_retries_migration_that_found_no_sip_bars(tmp_path):
    db = str(tmp_path / "bars.db")
    iex(Store(db))
    client = FakeClient()
    statuses = []
    p = feed.Poller(client, db, ["A"], poll_seconds=0, retry_seconds=0, on_status=statuses.append)
    p.on_progress = lambda d, t: client.data.update(A=[fake_bar(END, c=7.0)])  # empty first, bars on the retry
    p.on_updated = p.stop.set
    p.run()
    assert statuses == ["No SIP bars yet — retrying in 0s"]
    s = Store(db)
    assert s.meta("feed") == "sip" and [b.c for b in s.bars("A", 0)] == [7.0]


def test_migrate_fresh_install_swaps_in_what_it_fetched(tmp_path):
    s = Store(str(tmp_path / "bars.db"))
    assert feed.migrate(StrictClient(data={"A": [fake_bar(END)]}, now=NOW), s, ["A", "GONE"], now=NOW)
    assert s.latest_all() == {"A": int(END.timestamp())} and s.meta("feed") == "sip"


def test_upgrade_with_recent_iex_bars_never_inverts_a_window_or_mixes_feeds(tmp_path):
    db = str(tmp_path / "bars.db")
    now = datetime.now(timezone.utc)
    iex(Store(db), t=int((now - timedelta(minutes=2)).timestamp()))
    sip_bar = fake_bar(now - timedelta(minutes=20), c=7.0)
    statuses, updates = [], []
    p = feed.Poller(StrictClient(data={"A": [sip_bar]}), db, ["A"], poll_seconds=0, retry_seconds=0)
    p.on_status = lambda t: (statuses.append(t), p.stop.set())  # a rejected window would show as offline
    p.on_updated = lambda: (updates.append(1), len(updates) == 2 and p.stop.set())  # swap, then one backfill
    p.run()
    assert statuses == [] and len(updates) == 2
    s = Store(db)
    assert [(b.ts, b.c) for b in s.bars("A", 0)] == [(int(sip_bar.timestamp.timestamp()), 7.0)]
    assert s.meta("feed") == "sip"


def test_poller_does_not_backfill_or_repair_before_migration_completes(tmp_path, monkeypatch):
    monkeypatch.setattr(feed, "backfill", lambda *a, **k: pytest.fail("backfill during migration"))
    monkeypatch.setattr(feed, "repair_splits", lambda *a, **k: pytest.fail("split repair during migration"))
    db = str(tmp_path / "bars.db")
    old = int(time.time()) - 40 * 86400  # prune would drop it
    iex(Store(db), t=old)
    p = feed.Poller(FakeClient(data={x: [fake_bar(NOW)] for x in syms(25)}), db, syms(25),
                    actions=FakeActions([("S000", NOW.date())]), poll_seconds=0)
    p.on_progress = lambda d, t: p.stop.set()  # stop after the first batch
    p.run()
    s = Store(db)
    assert s.latest_all() == {"A": old} and s.meta("feed") is None and s.staged_latest_all()


class CountingStop(threading.Event):
    """Records each wait and returns at once; sets itself after `n` waits."""
    def __init__(self, n):
        super().__init__()
        self.n, self.waits = n, []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if len(self.waits) == self.n:
            self.set()
        return self.is_set()


def test_empty_sip_retry_backs_off_to_an_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(feed, "migrate", lambda *a, **k: False)  # SIP keeps returning nothing
    db = str(tmp_path / "bars.db")
    iex(Store(db))
    p = feed.Poller(FakeClient(), db, ["A"], retry_seconds=30)
    p.stop = CountingStop(9)
    p.run()
    assert p.stop.waits == [30, 60, 120, 240, 480, 960, 1920, 3600, 3600]


def test_fresh_install_backfills_without_migrating(tmp_path, monkeypatch):
    monkeypatch.setattr(feed, "migrate", lambda *a, **k: pytest.fail("fresh install staged a migration"))
    db = str(tmp_path / "bars.db")
    p = feed.Poller(StrictClient(data={"A": [fake_bar(datetime.now(timezone.utc) - timedelta(minutes=20))]}),
                    db, ["A"], poll_seconds=0)
    p.on_updated = p.stop.set
    p.run()
    s = Store(db)
    assert s.meta("feed") == "sip" and len(s.bars("A", 0)) == 1 and s.staged_latest_all() == {}


def test_interrupted_fresh_migration_still_resumes_from_staging(tmp_path):
    db = str(tmp_path / "bars.db")
    Store(db).upsert_staged([Bar("A", NOW_TS - 3600, 1.0, 1.0, 1.0, 1.0, 1)])  # left by an older version
    p = feed.Poller(FakeClient(), db, ["A"], poll_seconds=0)
    p.on_updated = p.stop.set
    p.run()
    s = Store(db)
    assert s.meta("feed") == "sip" and s.latest_all() == {"A": NOW_TS - 3600} and s.staged_latest_all() == {}
