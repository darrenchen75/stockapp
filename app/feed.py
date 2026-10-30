import csv
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alpaca.data.enums import DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from PySide6.QtCore import QObject, Signal

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
