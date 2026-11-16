import csv
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypeVar

from alpaca.data.enums import Adjustment, CorporateActionsType, DataFeed
from alpaca.data.requests import CorporateActionsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from app import market
from app.store import ET, Bar, Store

log = logging.getLogger(__name__)

T = TypeVar("T")

DAYS = 30                    # the chart's 30D window and the Gaps column
KEEP_DAYS = 35               # minute bars kept: 30 days plus a reference close for "Change 30D"
DELAY = timedelta(minutes=15)  # the free plan rejects SIP data newer than this
MARGIN = timedelta(minutes=1)  # slack for a local clock running ahead of Alpaca's
MAX_SYMBOLS = 100            # symbols per request
MAX_SPAN = 10 * DAYS * 1440  # symbol-minutes per request (10 symbols x 30 days, ~120k bars) bounds memory
RETRIES = 5
MAX_WAIT = 60                # seconds; Alpaca's limit window is one minute


def load_symbols() -> list[str]:
    with (Path(__file__).parent / "sp500.csv").open(newline="") as f:
        return [row["Symbol"] for row in csv.DictReader(f)]


def plan(symbols, latest, now, include_empty):
    """Group symbols into requests ending at `now`, oldest first. Each request starts at its earliest
    member's last bar (refetching it is harmless: upsert), never before the KEEP_DAYS floor.
    Symbols with no rows get KEEP_DAYS only when include_empty is set (first run). Symbols whose
    last bar is at or after `now` are skipped: their window would be empty or inverted."""
    now_ts = int(now.timestamp())
    floor = now_ts - KEEP_DAYS * 86400
    starts = {s: max(latest.get(s, floor), floor) for s in symbols if include_empty or s in latest}
    starts = {s: t for s, t in starts.items() if t < now_ts}
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


def with_retries(call: Callable[[], T], sleep=time.sleep) -> T:
    """Run `call`, waiting out 429s up to RETRIES times; any other error is raised."""
    for attempt in range(RETRIES):
        try:
            return call()
        except Exception as e:
            if getattr(e, "status_code", None) != 429 or attempt == RETRIES - 1:
                raise
            wait = retry_wait(e, attempt)
            log.warning("rate limited, retrying in %ss", wait)
            if sleep(wait):  # Event.wait is True once stopped: give up rather than retry at once
                raise


def fetch(client, symbols, start, end, sleep=time.sleep):
    req = StockBarsRequest(
        symbol_or_symbols=symbols, timeframe=TimeFrame.Minute,
        start=start, end=end, feed=DataFeed.SIP, adjustment=Adjustment.SPLIT,
    )
    data = with_retries(lambda: client.get_stock_bars(req).data, sleep)
    return [
        Bar(s, int(b.timestamp.timestamp()), b.open, b.high, b.low, b.close, int(b.volume))
        for s, bars in data.items() for b in bars
    ]


def backfill(client, store: Store, symbols, include_empty=True, now=None,
             sleep=time.sleep, progress=lambda done, total: None,
             stop: threading.Event | None = None) -> int:
    end = (now or datetime.now(timezone.utc)) - DELAY - MARGIN
    jobs = plan(symbols, store.latest_all(), end, include_empty)
    count = 0
    for i, (batch, start) in enumerate(jobs, 1):
        if stop and stop.is_set():
            break
        bars = fetch(client, batch, start, end, sleep)
        store.upsert(bars)  # per batch, so an interrupted backfill resumes where it stopped
        count += len(bars)
        progress(i, len(jobs))
    if include_empty:
        log_missing(symbols, store.latest_all())
    return count


def key_rejected(e) -> bool:
    """401/403, except a 403 about recent SIP data: the local clock ran ahead of Alpaca's, so retry."""
    status = getattr(e, "status_code", None)
    text = str(e).lower()
    return status == 401 or (status == 403 and not any(w in text for w in ("sip", "subscription", "recent")))


def log_missing(symbols, latest):
    missing = sorted(set(symbols) - latest.keys())
    if missing:
        log.info("no bars for %d symbols: %s", len(missing), ", ".join(missing))


def migrate(client, store: Store, symbols, now=None, sleep=time.sleep,
            progress=lambda done, total: None, stop: threading.Event | None = None) -> bool:
    """Download KEEP_DAYS of SIP bars into staging, then swap them in for the IEX bars in one
    transaction. Resumes from staging. False if stopped first, or if SIP gave no bars at all while IEX
    bars exist (nothing swapped, staging kept); errors propagate."""
    end = (now or datetime.now(timezone.utc)) - DELAY - MARGIN
    jobs = plan(symbols, store.staged_latest_all(), end, include_empty=True)
    for i, (batch, start) in enumerate(jobs, 1):
        if stop and stop.is_set():
            return False
        store.upsert_staged(fetch(client, batch, start, end, sleep))  # per batch: a restart resumes here
        progress(i, len(jobs))
    staged = store.staged_latest_all()
    if not staged and store.latest() is not None:
        log.warning("SIP returned no bars; keeping the IEX bars and retrying later")
        return False
    log_missing(symbols, staged)
    store.swap_staged()
    return True


def repair_splits(client, actions, store: Store, symbols, now=None, sleep=time.sleep,
                  stop: threading.Event | None = None) -> int:
    """Refetch KEEP_DAYS split-adjusted days for each symbol with an unhandled split in that window.
    The window is rolling so splits Alpaca posts late still get caught. A split is marked handled
    only after its ex-date, since Alpaca may not have adjusted history yet on the day itself.
    Returns how many splits are still pending: empty refetch, ex-date is today, or skipped by stop."""
    now = now or datetime.now(timezone.utc)
    end = now - DELAY - MARGIN
    today = now.astimezone(ET).date()
    data = actions.get_corporate_actions(CorporateActionsRequest(
        symbols=symbols, start=today - timedelta(days=KEEP_DAYS), end=today,
        types=[CorporateActionsType.FORWARD_SPLIT, CorporateActionsType.REVERSE_SPLIT],
    )).data
    found = {(a.symbol, a.ex_date.isoformat()) for actions_ in data.values() for a in actions_}
    pending = 0
    for symbol, ex_date in sorted(found - store.handled_splits()):
        if stop and stop.is_set():
            pending += 1
            continue
        bars = fetch(client, [symbol], now - timedelta(days=KEEP_DAYS), end, sleep)  # all kept days: replace drops the rest
        if not bars:
            log.warning("split refetch for %s came back empty; retrying in an hour", symbol)
            pending += 1
            continue
        confirmed = ex_date < today.isoformat()
        store.replace(symbol, bars, ex_date if confirmed else None)
        pending += not confirmed  # on the ex-date itself, retry hourly until Alpaca has adjusted history
        log.info("repaired %s for split on %s", symbol, ex_date)
    return pending


class Poller:
    """Runs backfill() on a background thread: full on start, then a gap fill every poll_seconds.
    Until the store's feed is SIP it runs only migrate(), so IEX and SIP bars never mix.
    Plain Python so it runs without a GUI; callbacks fire on the poller thread."""

    def __init__(self, client, db_path, symbols, actions=None, trading=None, poll_seconds=60, retry_seconds=30,
                 on_progress=lambda done, total: None, on_updated=lambda: None,
                 on_status=lambda text: None, on_auth_failed=lambda: None):
        self.client, self.db_path, self.symbols = client, db_path, symbols
        self.actions = actions  # corporate-actions client; None skips split repair
        self.trading = trading  # trading client for the market calendar; None skips it
        self.poll_seconds, self.retry_seconds = poll_seconds, retry_seconds
        self.on_progress, self.on_updated = on_progress, on_updated
        self.on_status, self.on_auth_failed = on_status, on_auth_failed
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def halt(self, timeout=60) -> bool:
        """Stop and wait for the thread; call off the UI thread. True if it ended."""
        self.stop.set()
        if self.thread:
            self.thread.join(timeout)
        return not (self.thread and self.thread.is_alive())

    def run(self):
        store = Store(self.db_path)  # own connection: sqlite3 connections stay on their thread
        first, pruned, next_split_check = True, 0.0, 0.0
        while not self.stop.is_set():
            try:
                if store.meta("feed") != "sip":
                    done = migrate(self.client, store, self.symbols, progress=self.on_progress,
                                   sleep=self.stop.wait, stop=self.stop)
                    if self.stop.is_set():
                        return  # the next launch resumes from staging
                    if not done:  # SIP had no bars at all: keep IEX, try again later
                        self.on_status(f"No SIP bars yet — retrying in {self.poll_seconds}s")
                        self.stop.wait(self.poll_seconds)
                        continue
                    self.on_updated()
                backfill(self.client, store, self.symbols, include_empty=first,
                         progress=self.on_progress, sleep=self.stop.wait, stop=self.stop)
            except Exception as e:
                if self.stop.is_set():
                    return  # the wait was cut short by stop; the error is just fallout
                if key_rejected(e):
                    log.warning("key rejected: %s", e)
                    self.on_auth_failed()
                    return
                log.exception("poll failed")
                self.on_status(f"Offline — retrying in {self.retry_seconds}s")
                self.stop.wait(self.retry_seconds)
                continue
            if self.stop.is_set():
                return
            first = False
            if self.trading:
                market.refresh(self.trading, store, datetime.now(ET))  # logs and swallows its own errors
            if time.time() - pruned > 86400:  # keep the cache a rolling KEEP_DAYS while the app stays open
                store.prune(KEEP_DAYS)
                pruned = time.time()
            if self.actions and not self.stop.is_set() and time.time() >= next_split_check:
                try:
                    pending = repair_splits(self.client, self.actions, store, self.symbols,
                                            sleep=self.stop.wait, stop=self.stop)
                except Exception:
                    if self.stop.is_set():
                        return
                    log.exception("split check failed")
                    pending = 1
                # Next New York midnight; within the hour if something failed, so mixed prices don't linger.
                tomorrow = datetime.now(ET).date() + timedelta(days=1)
                next_split_check = (time.time() + 3600 if pending
                                    else datetime.combine(tomorrow, datetime.min.time(), ET).timestamp())
            if self.stop.is_set():
                return
            self.on_updated()
            self.stop.wait(self.poll_seconds)
