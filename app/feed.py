import csv
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alpaca.data.enums import Adjustment, CorporateActionsType, DataFeed
from alpaca.data.requests import CorporateActionsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from app.store import ET, Bar, Store

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
        start=start, end=end, feed=DataFeed.IEX, adjustment=Adjustment.SPLIT,
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


def repair_splits(client, actions, store: Store, symbols, now=None, sleep=time.sleep) -> int:
    """Refetch 30 split-adjusted days for each symbol with an unhandled split in the last 30 days.
    The window is rolling so splits Alpaca posts late still get caught. Returns symbols repaired."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ET).date()
    data = actions.get_corporate_actions(CorporateActionsRequest(
        symbols=symbols, start=today - timedelta(days=DAYS), end=today,
        types=[CorporateActionsType.FORWARD_SPLIT, CorporateActionsType.REVERSE_SPLIT],
    )).data
    found = {(a.symbol, a.ex_date.isoformat()) for actions_ in data.values() for a in actions_}
    repaired = 0
    for symbol, ex_date in sorted(found - store.handled_splits()):
        bars = fetch(client, [symbol], now - timedelta(days=DAYS), now, sleep)
        if not bars:
            log.warning("split refetch for %s came back empty; retrying tomorrow", symbol)
            continue
        store.replace(symbol, bars, ex_date)
        log.info("repaired %s for split on %s", symbol, ex_date)
        repaired += 1
    return repaired


class Poller:
    """Runs backfill() on a background thread: full on start, then a gap fill every poll_seconds.
    Plain Python so it runs without a GUI; callbacks fire on the poller thread."""

    def __init__(self, client, db_path, symbols, actions=None, poll_seconds=60, retry_seconds=30,
                 on_progress=lambda done, total: None, on_updated=lambda: None,
                 on_status=lambda text: None, on_auth_failed=lambda: None):
        self.client, self.db_path, self.symbols = client, db_path, symbols
        self.actions = actions  # corporate-actions client; None skips split repair
        self.poll_seconds, self.retry_seconds = poll_seconds, retry_seconds
        self.on_progress, self.on_updated = on_progress, on_updated
        self.on_status, self.on_auth_failed = on_status, on_auth_failed
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        store = Store(self.db_path)  # own connection: sqlite3 connections stay on their thread
        first, pruned, split_day = True, 0.0, None
        while not self.stop.is_set():
            try:
                backfill(self.client, store, self.symbols, include_empty=first,
                         progress=self.on_progress)
            except Exception as e:
                if getattr(e, "status_code", None) in (401, 403):
                    log.warning("key rejected: %s", e)
                    self.on_auth_failed()
                    return
                log.exception("poll failed")
                self.on_status(f"Offline — retrying in {self.retry_seconds}s")
                self.stop.wait(self.retry_seconds)
                continue
            first = False
            if time.time() - pruned > 86400:  # keep the cache a rolling 30 days while the app stays open
                store.prune(DAYS)
                pruned = time.time()
            today = datetime.now(ET).date()
            if self.actions and today != split_day:  # once per New York day
                split_day = today  # set before the check: a failure retries tomorrow, not every poll
                try:
                    repair_splits(self.client, self.actions, store, self.symbols)
                except Exception:
                    log.exception("split check failed")
            self.on_updated()
            self.stop.wait(self.poll_seconds)
