# Stock Minute Bars Desktop App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A cross-platform PySide6 desktop app that keeps 30 days of S&P 500 minute bars from each user's own Alpaca key in a local SQLite cache and shows a live table plus candle chart.

**Architecture:** One process. `store.py` owns SQLite. `feed.py` plans and runs batched REST fetches (`backfill`) and wraps them in a background `Poller` (full backfill on launch, gap fill every 60s). `ui.py` renders from the store on each poll. `settings.py` keeps keys in the OS keychain. `main.py` wires it together. PyInstaller builds run in GitHub Actions on all three OSes.

**Tech Stack:** Python 3.12, uv, PySide6, pyqtgraph, alpaca-py (IEX feed), sqlite3, keyring, platformdirs, tzdata, pytest, PyInstaller.

**Spec:** `docs/superpowers/specs/2026-10-20-stock-minute-bars-design.md` (read the "Revision (during planning)" section: REST polling replaces the websocket).

## Global Constraints

- Python `>=3.12`; project managed with `uv`; run with `uv run python -m app.main`.
- Data source: Alpaca `StockHistoricalDataClient`, `feed=DataFeed.IEX`, `TimeFrame.Minute`. Each user brings their own key.
- History: rolling 30 days (`DAYS = 30`), pruned by the poller after its first successful poll and then daily, so a window left open for weeks stays bounded.
- Rate limit: stay under 200 requests/min (one poll is about 5 requests). On 429, wait `Retry-After` (else Alpaca's `X-RateLimit-Reset`, else exponential backoff), capped at 60s.
- Bar time `ts` = bar start, Unix seconds, UTC. "Session" = New York (`America/New_York`) calendar date, taken per symbol: a stale symbol (halt, delisting) shows its own last session vs. its own previous close.
- Keys are stored only in the OS keychain via `keyring`. Never write them to disk in plain text. With no keychain, keep them in memory for the session only.
- `tzdata` is a dependency, because Windows has no system tz database for `zoneinfo`.
- No order placement, no trade recommendations, no websocket, no `/clock` call.
- SQL lives only in `app/store.py`.
- Qt signals emitted from the poller thread must connect to **bound methods of QObjects** (window or status bar), never to lambdas, so Qt queues them onto the UI thread.

## Review Focus

1. **App closed during the first 30-day backfill:** the next launch resumes from each symbol's last stored bar and only symbols with no rows refetch 30 days. Test: `test_backfill_resumes_from_last_bar` (Task 2).
2. **Delisted or renamed symbol in the CSV:** it isn't refetched for 30 days on every 60s poll. Test: `test_plan_poll_skips_symbols_with_no_data` (Task 2).
3. **Reopening after weeks away** (every symbol stale by 20+ days): requests stay small enough not to blow memory. Test: `test_plan_bounds_request_size` (Task 2).
4. **Weekend or after-hours launch, and the EST date boundary:** the table compares the last session to the previous session's last bar, with the session date in New York time, not UTC. Tests: `test_summary_compares_last_session_to_previous_close`, `test_session_uses_new_york_date_not_utc` (Task 1).
5. **Linux with no Secret Service keychain:** no crash and no plain-text fallback. Test: `test_save_without_keychain_returns_false` (Task 3).

---

### Task 1: Project scaffold + SQLite store

**Files:**
- Create: `pyproject.toml` (via `uv`), `.gitignore`, `app/__init__.py`, `app/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces (in `app/store.py`):
  - `ET: ZoneInfo` (America/New_York)
  - `day_start(ts: int) -> int` (midnight New York time on the date of `ts`)
  - `@dataclass(frozen=True) Bar(symbol: str, ts: int, o: float, h: float, l: float, c: float, v: int)`
  - `@dataclass(frozen=True) Summary(last: float, prev_close: float | None, volume: int)` with property `pct -> float | None`
  - `class Store(path: str)` with:
    - `upsert(bars: Iterable[Bar]) -> None`
    - `latest_all() -> dict[str, int]`
    - `latest() -> int | None`
    - `bars(symbol: str, since: int) -> list[Bar]`
    - `prune(days: int = 30, now: float | None = None) -> int`
    - `session_start(symbol: str | None = None) -> int | None`
    - `summary() -> dict[str, Summary]`

- [ ] **Step 1: Scaffold the project**

```bash
cd /home/dchen/Code/stockapp
uv init --bare --python 3.12
uv add alpaca-py keyring platformdirs pyqtgraph PySide6 tzdata
uv add --dev pytest pyinstaller
mkdir -p app tests
touch app/__init__.py
cat >> pyproject.toml <<'EOF'

[tool.pytest.ini_options]
pythonpath = ["."]
EOF
cat > .gitignore <<'EOF'
.venv/
__pycache__/
build/
dist/
*.spec
EOF
```

- [ ] **Step 2: Write the failing tests**

`tests/test_store.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.store'`

- [ ] **Step 4: Implement `app/store.py`**

```python
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from datetime import time as clock
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def day_start(ts: int) -> int:
    """Midnight New York time on the date of `ts`."""
    day = datetime.fromtimestamp(ts, ET).date()
    return int(datetime.combine(day, clock(), ET).timestamp())


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: int  # bar start, Unix seconds UTC
    o: float
    h: float
    l: float
    c: float
    v: int


@dataclass(frozen=True)
class Summary:
    last: float
    prev_close: float | None
    volume: int

    @property
    def pct(self) -> float | None:
        if not self.prev_close:
            return None
        return (self.last - self.prev_close) / self.prev_close * 100


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")  # UI reads while the poller writes
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS bars("
            "symbol TEXT NOT NULL, ts INTEGER NOT NULL,"
            "o REAL, h REAL, l REAL, c REAL, v INTEGER,"
            "PRIMARY KEY(symbol, ts)) WITHOUT ROWID"
        )

    def upsert(self, bars: Iterable[Bar]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)",
                [(b.symbol, b.ts, b.o, b.h, b.l, b.c, b.v) for b in bars],
            )

    def latest_all(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT symbol, MAX(ts) FROM bars GROUP BY symbol"))

    def latest(self) -> int | None:
        return self.db.execute("SELECT MAX(ts) FROM bars").fetchone()[0]

    def bars(self, symbol: str, since: int) -> list[Bar]:
        rows = self.db.execute(
            "SELECT * FROM bars WHERE symbol = ? AND ts >= ? ORDER BY ts", (symbol, since)
        )
        return [Bar(*r) for r in rows]

    def prune(self, days: int = 30, now: float | None = None) -> int:
        cutoff = int(time.time() if now is None else now) - days * 86400
        with self.db:
            return self.db.execute("DELETE FROM bars WHERE ts < ?", (cutoff,)).rowcount

    def session_start(self, symbol: str | None = None) -> int | None:
        """Midnight New York time on the date of the newest bar (of `symbol`, if given)."""
        ts = self.latest() if symbol is None else self.db.execute(
            "SELECT MAX(ts) FROM bars WHERE symbol = ?", (symbol,)
        ).fetchone()[0]
        return None if ts is None else day_start(ts)

    def summary(self) -> dict[str, Summary]:
        """Each symbol's latest session vs. its own previous close (primary-key lookups)."""
        out = {}
        for s, ts in self.latest_all().items():
            start = day_start(ts)
            last = self.db.execute(
                "SELECT c FROM bars WHERE symbol = ? AND ts = ?", (s, ts)
            ).fetchone()[0]
            prev = self.db.execute(
                "SELECT c FROM bars WHERE symbol = ? AND ts < ? ORDER BY ts DESC LIMIT 1", (s, start)
            ).fetchone()
            volume = self.db.execute(
                "SELECT SUM(v) FROM bars WHERE symbol = ? AND ts >= ?", (s, start)
            ).fetchone()[0]
            out[s] = Summary(last, prev[0] if prev else None, volume)
        return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_store.py -v`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock .gitignore app/__init__.py app/store.py tests/test_store.py
git commit -m "feat: add SQLite bar store with session summary"
```

---

### Task 2: Feed — symbol list, request planning, fetch with retry, backfill

**Files:**
- Create: `app/sp500.csv`, `app/feed.py`
- Test: `tests/test_feed.py`

**Interfaces:**
- Consumes: `Bar`, `Store` (`upsert`, `latest_all`) from Task 1.
- Produces (in `app/feed.py`):
  - constants `DAYS = 30`, `MAX_SYMBOLS = 100`, `MAX_SPAN = 432_000`, `RETRIES = 5`, `MAX_WAIT = 60`
  - `retry_wait(error, attempt: int) -> float`
  - `load_symbols() -> list[str]`
  - `plan(symbols: list[str], latest: dict[str, int], now: datetime, include_empty: bool) -> list[tuple[list[str], datetime]]`
  - `fetch(client, symbols: list[str], start: datetime, end: datetime, sleep=time.sleep) -> list[Bar]`
  - `backfill(client, store: Store, symbols: list[str], include_empty: bool = True, now: datetime | None = None, sleep=time.sleep, progress=lambda done, total: None) -> int`
  - `client` is anything with `get_stock_bars(StockBarsRequest)` returning an object with `.data: dict[str, list[<obj with timestamp, open, high, low, close, volume>]]` (alpaca-py `StockHistoricalDataClient`, or a test fake). Errors carry an HTTP status as `.status_code` and response headers as `.response.headers` (alpaca-py `APIError`).

- [ ] **Step 1: Bundle the S&P 500 list**

```bash
curl -fsSL https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv -o app/sp500.csv
head -2 app/sp500.csv   # expect header starting "Symbol,Security,"
```

Class-B tickers use dots (`BRK.B`, `BF.B`), which matches Alpaca's format. Leave them as-is.

- [ ] **Step 2: Write the failing tests**

`tests/test_feed.py`:

```python
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import feed
from app.store import Bar, Store

NOW = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)
NOW_TS = int(NOW.timestamp())
FLOOR = NOW - timedelta(days=30)


def fake_bar(t: datetime, c=10.0):
    return SimpleNamespace(timestamp=t, open=c, high=c, low=c, close=c, volume=100.0)


class HTTPError(Exception):
    def __init__(self, status, headers=None):
        super().__init__(f"HTTP {status}")
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


def test_plan_first_run_fetches_30_days_in_batches_of_10():
    jobs = feed.plan(syms(25), {}, NOW, include_empty=True)
    assert [len(b) for b, _ in jobs] == [10, 10, 5]
    assert all(start == FLOOR for _, start in jobs)


def test_plan_poll_batches_known_symbols_by_100():
    latest = {s: NOW_TS - 120 for s in syms(250)}
    jobs = feed.plan(list(latest), latest, NOW, include_empty=False)
    assert [len(b) for b, _ in jobs] == [100, 100, 50]
    assert all(start == NOW - timedelta(seconds=120) for _, start in jobs)


def test_plan_poll_skips_symbols_with_no_data():
    jobs = feed.plan(["AAPL", "GONE"], {"AAPL": NOW_TS - 60}, NOW, include_empty=False)
    assert [b for b, _ in jobs] == [["AAPL"]]


def test_plan_never_starts_before_30_day_floor():
    jobs = feed.plan(["AAPL"], {"AAPL": NOW_TS - 40 * 86400}, NOW, include_empty=False)
    assert jobs == [(["AAPL"], FLOOR)]


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


def test_backfill_resumes_from_last_bar(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    t1, t2 = NOW - timedelta(minutes=2), NOW - timedelta(minutes=1)
    client = FakeClient({"AAPL": [fake_bar(t1), fake_bar(t2)]})
    progress = []
    n = feed.backfill(client, store, ["AAPL", "GONE"], now=NOW, progress=lambda d, t: progress.append((d, t)))
    assert n == 2 and progress == [(1, 1)]
    assert store.latest_all() == {"AAPL": int(t2.timestamp())}

    feed.backfill(client, store, ["AAPL", "GONE"], include_empty=False, now=NOW)
    resumed = client.requests[-1]
    assert resumed.symbol_or_symbols == ["AAPL"]
    assert utc(resumed.start) == t2
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_feed.py -v`
Expected: FAIL with `ImportError: cannot import name 'feed' from 'app'`

- [ ] **Step 4: Implement `app/feed.py`**

```python
import csv
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alpaca.data.enums import DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from app.store import Bar, Store

log = logging.getLogger(__name__)

DAYS = 30
MAX_SYMBOLS = 100            # symbols per request
MAX_SPAN = 10 * DAYS * 1440  # symbol-minutes per request (10 symbols x 30 days, ~120k bars) bounds memory
RETRIES = 5
MAX_WAIT = 60                # seconds; Alpaca's limit window is one minute


def load_symbols() -> list[str]:
    with (Path(__file__).parent / "sp500.csv").open(newline="") as f:
        return [row["Symbol"] for row in csv.DictReader(f)]


def plan(symbols, latest, now, include_empty):
    """Group symbols into requests, oldest first. Each request starts at its earliest
    member's last bar (refetching it is harmless: upsert), never before the 30-day floor.
    Symbols with no rows get 30 days only when include_empty is set (first run)."""
    now_ts = int(now.timestamp())
    floor = now_ts - DAYS * 86400
    starts = {s: max(latest.get(s, floor), floor) for s in symbols if include_empty or s in latest}
    jobs, batch = [], []
    for s in sorted(starts, key=starts.get):
        if batch:
            minutes = max(1, (now_ts - starts[batch[0]]) // 60)
            if len(batch) == MAX_SYMBOLS or (len(batch) + 1) * minutes > MAX_SPAN:
                jobs.append(batch)
                batch = []
        batch.append(s)
    if batch:
        jobs.append(batch)
    return [(b, datetime.fromtimestamp(starts[b[0]], timezone.utc)) for b in jobs]


def retry_wait(e, attempt):
    """Seconds to wait after a 429: Retry-After, else Alpaca's X-RateLimit-Reset (epoch),
    else exponential backoff."""
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    try:
        if "Retry-After" in headers:
            return min(float(headers["Retry-After"]), MAX_WAIT)
        if "X-RateLimit-Reset" in headers:
            return min(max(float(headers["X-RateLimit-Reset"]) - time.time(), 1), MAX_WAIT)
    except ValueError:
        pass  # shortcut: HTTP-date Retry-After falls back to backoff; parse it if Alpaca ever sends one
    return 5 * 2**attempt


def fetch(client, symbols, start, end, sleep=time.sleep):
    req = StockBarsRequest(
        symbol_or_symbols=symbols, timeframe=TimeFrame.Minute,
        start=start, end=end, feed=DataFeed.IEX,
    )
    for attempt in range(RETRIES):
        try:
            data = client.get_stock_bars(req).data
            break
        except Exception as e:
            if getattr(e, "status_code", None) != 429 or attempt == RETRIES - 1:
                raise
            wait = retry_wait(e, attempt)
            log.warning("rate limited, retrying in %ss", wait)
            sleep(wait)
    return [
        Bar(s, int(b.timestamp.timestamp()), b.open, b.high, b.low, b.close, int(b.volume))
        for s, bars in data.items() for b in bars
    ]


def backfill(client, store: Store, symbols, include_empty=True, now=None,
             sleep=time.sleep, progress=lambda done, total: None) -> int:
    now = now or datetime.now(timezone.utc)
    jobs = plan(symbols, store.latest_all(), now, include_empty)
    count = 0
    for i, (batch, start) in enumerate(jobs, 1):
        bars = fetch(client, batch, start, now, sleep)
        store.upsert(bars)  # per batch, so an interrupted backfill resumes where it stopped
        count += len(bars)
        progress(i, len(jobs))
    if include_empty:
        missing = sorted(set(symbols) - store.latest_all().keys())
        if missing:
            log.info("no bars for %d symbols: %s", len(missing), ", ".join(missing))
    return count
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: all pass (9 store + 15 feed)

- [ ] **Step 6: Commit**

```bash
git add app/sp500.csv app/feed.py tests/test_feed.py
git commit -m "feat: add batched Alpaca backfill with rate-limit retry"
```

---

### Task 3: Keychain settings + key dialog

**Files:**
- Create: `app/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (in `app/settings.py`):
  - `load_keys() -> tuple[str, str] | None`
  - `save_keys(key: str, secret: str) -> bool` (False = no keychain, so keys stay in memory only)
  - `ask_keys(parent=None, message: str = "") -> tuple[str, str] | None` (modal dialog; None = cancelled)
  - `remember(parent, keys: tuple[str, str]) -> None` (save + warn if no keychain)

- [ ] **Step 1: Write the failing tests**

`tests/test_settings.py`:

```python
import keyring
from keyring.errors import NoKeyringError

from app import settings


def test_round_trip(monkeypatch):
    vault = {}
    monkeypatch.setattr(keyring, "set_password", lambda svc, user, pw: vault.__setitem__((svc, user), pw))
    monkeypatch.setattr(keyring, "get_password", lambda svc, user: vault.get((svc, user)))
    assert settings.load_keys() is None
    assert settings.save_keys("KEY", "SECRET") is True
    assert settings.load_keys() == ("KEY", "SECRET")


def test_save_without_keychain_returns_false(monkeypatch):
    def no_keychain(*args):
        raise NoKeyringError("no backend")

    monkeypatch.setattr(keyring, "set_password", no_keychain)
    monkeypatch.setattr(keyring, "get_password", no_keychain)
    assert settings.save_keys("KEY", "SECRET") is False
    assert settings.load_keys() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_settings.py -v`
Expected: FAIL with `ImportError: cannot import name 'settings' from 'app'`

- [ ] **Step 3: Implement `app/settings.py`**

```python
import logging

import keyring
from keyring.errors import KeyringError
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QLabel,
                               QLineEdit, QMessageBox)

log = logging.getLogger(__name__)
SERVICE = "stockapp"


def load_keys() -> tuple[str, str] | None:
    try:
        key = keyring.get_password(SERVICE, "key_id")
        secret = keyring.get_password(SERVICE, "secret")
    except KeyringError as e:
        log.warning("keychain unavailable: %s", e)
        return None
    return (key, secret) if key and secret else None


def save_keys(key: str, secret: str) -> bool:
    try:
        keyring.set_password(SERVICE, "key_id", key)
        keyring.set_password(SERVICE, "secret", secret)
    except KeyringError as e:
        log.warning("keychain unavailable, keys kept for this session only: %s", e)
        return False
    return True


def remember(parent, keys: tuple[str, str]) -> None:
    if not save_keys(*keys):
        QMessageBox.warning(parent, "No keychain",
                            "No system keychain was found, so your key is used for this "
                            "session only and you'll be asked again next launch.")


def ask_keys(parent=None, message: str = "") -> tuple[str, str] | None:
    dialog = QDialog(parent)
    dialog.setWindowTitle("Alpaca API key")
    form = QFormLayout(dialog)
    info = QLabel((message + "<br><br>" if message else "") +
                  'Paste a free API key from <a href="https://app.alpaca.markets">'
                  "app.alpaca.markets</a> (a paper-trading key works).")
    info.setOpenExternalLinks(True)
    info.setWordWrap(True)
    form.addRow(info)
    key = QLineEdit()
    secret = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
    form.addRow("Key ID", key)
    form.addRow("Secret", secret)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    form.addRow(buttons)
    if dialog.exec() != QDialog.Accepted:
        return None
    k, s = key.text().strip(), secret.text().strip()
    return (k, s) if k and s else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add app/settings.py tests/test_settings.py
git commit -m "feat: store Alpaca keys in OS keychain"
```

---

### Task 4: Poller, main window, entry point

**Files:**
- Modify: `app/feed.py` (append `Poller`)
- Create: `app/ui.py`, `app/main.py`
- Test: `tests/test_feed.py` (append poller tests)

**Interfaces:**
- Consumes: `Store` (all methods), `ET`, `Bar` (Task 1); `backfill`, `load_symbols` (Task 2); `settings.load_keys/ask_keys/remember` (Task 3).
- Produces:
  - `feed.Poller(client, db_path: str, symbols: list[str], poll_seconds=60, retry_seconds=30)`, a `QObject` with signals `progress(int, int)`, `updated()`, `status(str)`, `auth_failed()`; attribute `stop: threading.Event`; methods `start()` (daemon thread) and `run()` (blocking loop).
  - `ui.MainWindow(db_path: str, symbols: list[str], keys: tuple[str, str])`
  - `main.main() -> int`

- [ ] **Step 1: Write the failing poller tests** (append to `tests/test_feed.py`)

```python
def test_poller_stops_on_rejected_key(tmp_path):
    p = feed.Poller(FakeClient(errors=[HTTPError(401)]), str(tmp_path / "bars.db"), ["AAPL"])
    seen = []
    p.auth_failed.connect(lambda: seen.append("auth"))
    p.run()  # must return instead of looping
    assert seen == ["auth"]


def test_poller_reports_offline_and_retries(tmp_path):
    client = FakeClient(errors=[ConnectionError("down")])
    p = feed.Poller(client, str(tmp_path / "bars.db"), ["AAPL"], poll_seconds=0, retry_seconds=0)
    statuses = []
    p.status.connect(statuses.append)
    p.updated.connect(p.stop.set)  # stop after the first successful poll
    p.run()
    assert statuses == ["Offline — retrying in 0s"]
    assert len(client.requests) == 2


def test_poller_prunes_while_running(tmp_path):
    db = str(tmp_path / "bars.db")
    old = int(time.time()) - 31 * 86400
    Store(db).upsert([Bar("AAPL", old, 1.0, 1.0, 1.0, 1.0, 1)])
    p = feed.Poller(FakeClient(), db, ["AAPL"], poll_seconds=0)
    p.updated.connect(p.stop.set)
    p.run()
    assert Store(db).latest_all() == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_feed.py -k poller -v`
Expected: FAIL with `AttributeError: module 'app.feed' has no attribute 'Poller'`

- [ ] **Step 3: Implement `Poller`**

Add `import threading` and `from PySide6.QtCore import QObject, Signal` to the imports in `app/feed.py`, then append:

```python
class Poller(QObject):
    """Runs backfill() on a background thread: full on start, then a gap fill every poll_seconds."""

    progress = Signal(int, int)
    updated = Signal()
    status = Signal(str)
    auth_failed = Signal()

    def __init__(self, client, db_path, symbols, poll_seconds=60, retry_seconds=30):
        super().__init__()
        self.client, self.db_path, self.symbols = client, db_path, symbols
        self.poll_seconds, self.retry_seconds = poll_seconds, retry_seconds
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        store = Store(self.db_path)  # own connection: sqlite3 connections stay on their thread
        first, pruned = True, 0.0
        while not self.stop.is_set():
            try:
                backfill(self.client, store, self.symbols, include_empty=first,
                         progress=self.progress.emit)
            except Exception as e:
                if getattr(e, "status_code", None) in (401, 403):
                    log.warning("key rejected: %s", e)
                    self.auth_failed.emit()
                    return
                log.exception("poll failed")
                self.status.emit(f"Offline — retrying in {self.retry_seconds}s")
                self.stop.wait(self.retry_seconds)
                continue
            first = False
            if time.time() - pruned > 86400:  # keep the cache a rolling 30 days while the app stays open
                store.prune(DAYS)
                pruned = time.time()
            self.updated.emit()
            self.stop.wait(self.poll_seconds)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: all pass

- [ ] **Step 5: Implement `app/ui.py`**

```python
import time
from datetime import datetime

import pyqtgraph as pg
from alpaca.data.historical import StockHistoricalDataClient
from PySide6.QtCore import QPointF, QRectF, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QPainter, QPicture, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QHBoxLayout, QMainWindow,
                               QPushButton, QSplitter, QTableView, QVBoxLayout, QWidget)

from app import settings
from app.feed import Poller
from app.store import ET, Bar, Store

COLUMNS = ["Symbol", "Last", "% Chg", "Volume"]
RANGES = {"1D": None, "5D": 5, "30D": 30}  # None = since the start of the latest session
UP, DOWN = QColor("#26a69a"), QColor("#ef5350")
LIVE_SECONDS = 300


class Candles(pg.GraphicsObject):
    """Minute candles drawn once into a QPicture (pyqtgraph's custom-item pattern)."""

    def __init__(self, bars: list[Bar]):
        super().__init__()
        self.bounds = QRectF(bars[0].ts, min(b.l for b in bars),
                             bars[-1].ts + 60 - bars[0].ts,
                             max(b.h for b in bars) - min(b.l for b in bars))
        self.picture = QPicture()
        p = QPainter(self.picture)
        for b in bars:
            color = UP if b.c >= b.o else DOWN
            p.setPen(pg.mkPen(color))
            p.setBrush(pg.mkBrush(color))
            mid = b.ts + 30
            p.drawLine(QPointF(mid, b.l), QPointF(mid, b.h))
            p.drawRect(QRectF(mid - 20, b.o, 40, b.c - b.o))
        p.end()

    def paint(self, painter, *args):
        painter.drawPicture(0, 0, self.picture)

    def boundingRect(self):
        return self.bounds


class MainWindow(QMainWindow):
    def __init__(self, db_path: str, symbols: list[str], keys: tuple[str, str]):
        super().__init__()
        self.setWindowTitle("Stock Bars")
        self.db_path, self.symbols = db_path, symbols
        self.store = Store(db_path)
        self.symbol: str | None = None
        self.range = "1D"
        self.poller: Poller | None = None

        self.model = QStandardItemModel(0, len(COLUMNS))
        self.model.setHorizontalHeaderLabels(COLUMNS)
        for s in symbols:
            self.model.appendRow([QStandardItem(s)] + [QStandardItem() for _ in COLUMNS[1:]])
        self.rows = {s: i for i, s in enumerate(symbols)}
        self.proxy = QSortFilterProxyModel()  # re-sorts on data change; source rows stay put
        self.proxy.setSourceModel(self.model)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.selectionModel().currentRowChanged.connect(self.on_select)

        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        buttons = QHBoxLayout()
        group = QButtonGroup(self)
        for name in RANGES:
            button = QPushButton(name, checkable=True, checked=name == self.range)
            button.clicked.connect(lambda _=False, n=name: self.set_range(n))
            group.addButton(button)
            buttons.addWidget(button)
        buttons.addStretch()
        right = QWidget()
        column = QVBoxLayout(right)
        column.addLayout(buttons)
        column.addWidget(self.plot)

        split = QSplitter()
        split.addWidget(self.table)
        split.addWidget(right)
        split.setSizes([420, 780])
        self.setCentralWidget(split)

        self.refresh()
        self.start_poller(keys)

    def start_poller(self, keys: tuple[str, str]):
        self.poller = Poller(StockHistoricalDataClient(*keys), self.db_path, self.symbols)
        # Bound methods of QObjects on the UI thread: Qt queues these cross-thread calls.
        self.poller.progress.connect(self.on_progress)
        self.poller.updated.connect(self.refresh)
        self.poller.status.connect(self.statusBar().showMessage)
        self.poller.auth_failed.connect(self.on_auth_failed)
        self.poller.start()

    def on_progress(self, done: int, total: int):
        self.statusBar().showMessage(f"Fetching bars… {done}/{total}")

    def on_auth_failed(self):
        keys = settings.ask_keys(self, "Alpaca rejected that key. Check it and try again.")
        if keys is None:
            self.close()
            return
        settings.remember(self, keys)
        self.start_poller(keys)

    def refresh(self):
        summaries = self.store.summary()
        for symbol, row in self.rows.items():
            x = summaries.get(symbol)
            pct = x.pct if x else None
            values = [round(x.last, 2), None if pct is None else round(pct, 2), x.volume] if x else [None] * 3
            for col, value in enumerate(values, start=1):
                self.model.item(row, col).setData(value, Qt.DisplayRole)  # None = blank cell
            color = None if pct is None else (UP if pct >= 0 else DOWN)
            self.model.item(row, 2).setData(color, Qt.ForegroundRole)
        self.show_status()
        self.draw()

    def show_status(self):
        ts = self.store.latest()
        if ts is None:
            self.statusBar().showMessage("No data yet")
            return
        when = datetime.fromtimestamp(ts, ET).strftime("%a %b %d %H:%M ET")
        state = "Live" if time.time() - ts < LIVE_SECONDS else "Market closed"
        self.statusBar().showMessage(f"{state} · last bar {when}")

    def on_select(self, current, _previous):
        if not current.isValid():
            return
        self.symbol = self.model.item(self.proxy.mapToSource(current).row(), 0).text()
        self.draw()

    def set_range(self, name: str):
        self.range = name
        self.draw()

    def draw(self):
        self.plot.clear()
        if self.symbol is None:
            return
        days = RANGES[self.range]
        since = (self.store.session_start(self.symbol) or 0) if days is None else int(time.time()) - days * 86400
        bars = self.store.bars(self.symbol, since)
        self.plot.setTitle(f"{self.symbol} · {self.range}")
        if bars:
            self.plot.addItem(Candles(bars))

    def closeEvent(self, event):
        if self.poller:
            self.poller.stop.set()
        super().closeEvent(event)
```

- [ ] **Step 6: Implement `app/main.py`**

```python
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from platformdirs import user_data_dir, user_log_dir
from PySide6.QtWidgets import QApplication

from app import settings
from app.feed import load_symbols
from app.ui import MainWindow

APP = "stockapp"


def main() -> int:
    log_dir = Path(user_log_dir(APP, appauthor=False))
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[RotatingFileHandler(log_dir / "stockapp.log", maxBytes=1_000_000, backupCount=3)],
    )
    data_dir = Path(user_data_dir(APP, appauthor=False))
    data_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    keys = settings.load_keys()
    if keys is None:
        keys = settings.ask_keys()
        if keys is None:
            return 0
        settings.remember(None, keys)

    window = MainWindow(str(data_dir / "bars.db"), load_symbols(), keys)
    window.resize(1200, 700)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7: Manual check**

Run: `uv run python -m app.main`

If Qt says `could not load the Qt platform plugin "xcb"`, run `sudo apt install libxcb-cursor0` and retry.

Expected:
1. The key dialog appears. Paste a real Alpaca key and secret, then click OK.
2. The window shows a table of about 503 symbols with blank cells, and the status bar shows `Fetching bars… n/N`, which counts up for about 2–5 minutes.
3. Once that finishes, the cells fill in and the status reads `Live · last bar …` during market hours or `Market closed · last bar …` otherwise.
4. Click a symbol: candles draw. 1D, 5D and 30D switch the range. Clicking the "% Chg" header sorts numerically.
5. Close and relaunch: no key prompt, the table fills from the cache right away, and the poll finishes in seconds.
6. Bad-key path: `uv run python -c "import keyring; keyring.set_password('stockapp','secret','wrong')"`, then launch. Expected: the "Alpaca rejected that key" dialog appears.
7. The log file exists: `ls ~/.local/state/stockapp/log/` (Linux path from platformdirs).

- [ ] **Step 8: Commit**

```bash
git add app/feed.py app/ui.py app/main.py tests/test_feed.py
git commit -m "feat: add poller, main window, and entry point"
```

---

### Task 5: README, release workflow, local build check

**Files:**
- Create: `README.md`, `.github/workflows/release.yml`

**Interfaces:**
- Consumes: `app/main.py` entry point and `app/sp500.csv` (bundled as data).
- Produces: the release artifacts `StockBars-macos.dmg`, `StockBars-windows.zip` and `StockBars-linux.tar.gz`.

- [ ] **Step 1: Local PyInstaller build on Linux**

```bash
uv run pyinstaller --noconfirm --windowed --onefile --name StockBars --paths . \
  --add-data "app/sp500.csv:app" --collect-submodules keyring.backends app/main.py
./dist/StockBars
```

Expected: the same behavior as Task 4 step 7, items 1–5 (your saved key is reused from the keychain). If it fails with `ModuleNotFoundError`, add `--hidden-import <module>` to this command **and** to the workflow below, then rebuild.

- [ ] **Step 2: Write `.github/workflows/release.yml`**

```yaml
name: release

on:
  push:
    tags: ["v*"]

permissions:
  contents: write

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: sudo apt-get update && sudo apt-get install -y libegl1 libxkbcommon0
      - run: uv sync
      - run: uv run pytest -q

  build:
    needs: test
    strategy:
      matrix:
        include:
          - os: macos-latest   # Apple Silicon; add macos-13 for Intel Macs
            mode: --onedir     # .app bundle, wrapped in a .dmg below
          - os: windows-latest
            mode: --onefile
          - os: ubuntu-latest
            mode: --onefile
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync
      - run: >
          uv run pyinstaller --noconfirm --windowed ${{ matrix.mode }} --name StockBars --paths .
          --add-data "app/sp500.csv:app" --collect-submodules keyring.backends app/main.py
      - name: Package macOS
        if: runner.os == 'macOS'
        run: hdiutil create -volname StockBars -srcfolder dist/StockBars.app -ov -format UDZO StockBars-macos.dmg
      - name: Package Windows
        if: runner.os == 'Windows'
        run: Compress-Archive -Path dist/StockBars.exe -DestinationPath StockBars-windows.zip
      - name: Package Linux
        if: runner.os == 'Linux'
        run: tar czf StockBars-linux.tar.gz -C dist StockBars
      - uses: softprops/action-gh-release@v2
        with:
          files: StockBars-*
```

- [ ] **Step 3: Write `README.md`**

````markdown
# Stock Bars

Minute bars for the S&P 500 on your desktop. Keeps 30 days of history on this
machine and updates every minute during market hours. Shows data only. It never
places trades.

## Get a key

1. Sign up free at https://app.alpaca.markets.
2. Create an API key (a paper-trading key works).
3. Paste the key and secret when the app asks. They are stored in your OS keychain.

The free plan uses the IEX exchange feed. Prices are representative, but volume
is IEX-only, so it's lower than the whole-market volume you see elsewhere.

## Install

Download the file for your system from the latest GitHub Release.

- **macOS** (Apple Silicon): open `StockBars-macos.dmg` and drag the app to
  Applications. The first time, right-click the app, choose Open, then confirm.
  (The app is unsigned.)
- **Windows**: unzip and run `StockBars.exe`. If SmartScreen warns, click
  More info, then Run anyway.
- **Linux**: `tar xzf StockBars-linux.tar.gz && ./StockBars`. If Qt
  complains about the "xcb" plugin, run `sudo apt install libxcb-cursor0`.

## Problems

Send the log file:

- macOS: `~/Library/Logs/stockapp/`
- Windows: `%LOCALAPPDATA%\stockapp\Logs\`
- Linux: `~/.local/state/stockapp/log/`

## Develop

```
uv sync
uv run pytest
uv run python -m app.main
```

To release, push a version tag (`git tag v0.1.0 && git push --tags`). GitHub
Actions then builds all three installers onto a Release.

To update the symbol list:
`curl -fsSL https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv -o app/sp500.csv`
````

- [ ] **Step 4: Run the full test suite one last time**

Run: `uv run pytest -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add README.md .github/workflows/release.yml
git commit -m "build: add README and cross-platform release workflow"
```

Pushing a tag to run the workflow requires a GitHub remote. That's the user's call: ask before creating a repo or pushing.
