import logging
import time
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from datetime import time as clock
from pathlib import Path

import pandas as pd
from alpaca.data.enums import Adjustment, CorporateActionsType, DataFeed
from alpaca.data.requests import CorporateActionsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from platformdirs import user_data_dir

from app.feed import MAX_SYMBOLS, load_symbols, with_retries
from app.store import ET, DailyBar, Store

log = logging.getLogger(__name__)

HISTORY_START = date(2016, 1, 1)
CUTOFF = clock(20, 30)  # a day's bar is final, and visible to strategies, from 20:30 New York
COLUMNS = ["symbol", "date", "o", "h", "l", "c", "v", "factor_all", "factor_split"]


def decision_time(day: date) -> datetime:
    return datetime.combine(day, CUTOFF, ET)


def last_final_date(now: datetime) -> date:
    local = now.astimezone(ET)
    return local.date() if local.time() >= CUTOFF else local.date() - timedelta(days=1)


def universe() -> list[str]:
    return load_symbols() + ["SPY"]


def default_db_path() -> str:
    path = Path(user_data_dir("stockapp", appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return str(path / "bars.db")


def build_factors(closes: list[tuple[str, float]], splits: list[tuple[str, float]],
                  dividends: list[tuple[str, float]]) -> list[tuple[float, float]]:
    """(factor_all, factor_split) per close. Each event scales every day before its ex-date.
    A dividend d scales by 1 - d / C, C being the raw close of the last day before the ex-date."""
    dates = [d for d, _ in closes]
    split_at, div_at = defaultdict(lambda: 1.0), defaultdict(lambda: 1.0)
    for ex, ratio in splits:
        split_at[ex] *= ratio
    for ex, amount in dividends:
        i = bisect_left(dates, ex)  # closes before the ex-date are closes[:i]
        if i:
            div_at[ex] *= 1 - amount / closes[i - 1][1]
    events = sorted(set(split_at) | set(div_at), reverse=True)
    out, split_f, all_f, e = [], 1.0, 1.0, 0
    for d in reversed(dates):
        while e < len(events) and events[e] > d:
            split_f *= split_at[events[e]]
            all_f *= split_at[events[e]] * div_at[events[e]]
            e += 1
        out.append((all_f, split_f))
    return out[::-1]


def parse_actions(data) -> dict[str, tuple[list, list]]:
    """symbol -> (splits, dividends) as build_factors takes them. Other action types are ignored."""
    out = defaultdict(lambda: ([], []))
    for key in ("forward_splits", "reverse_splits"):
        for a in data.get(key, []):
            out[a.symbol][0].append((a.ex_date.isoformat(), a.old_rate / a.new_rate))
    for a in data.get("cash_dividends", []):  # all of them: Alpaca's `special` flag is unreliable
        out[a.symbol][1].append((a.ex_date.isoformat(), a.rate))
    return out


def batches(client, actions, symbols, through: date, sleep=time.sleep):
    """Yield {symbol: bars} for each batch of symbols, with both requests done before it is yielded."""
    start = datetime.combine(HISTORY_START, clock(), ET)
    end = decision_time(through) - timedelta(minutes=15)  # SIP data newer than 15 minutes is not allowed
    for i in range(0, len(symbols), MAX_SYMBOLS):
        batch = symbols[i:i + MAX_SYMBOLS]
        bars = with_retries(lambda: client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=batch, timeframe=TimeFrame.Day, start=start, end=end,
            feed=DataFeed.SIP, adjustment=Adjustment.RAW)).data, sleep)
        acts = parse_actions(with_retries(lambda: actions.get_corporate_actions(CorporateActionsRequest(
            symbols=batch, start=HISTORY_START, end=through,
            types=[CorporateActionsType.FORWARD_SPLIT, CorporateActionsType.REVERSE_SPLIT,
                   CorporateActionsType.CASH_DIVIDEND])).data, sleep))
        out = {}
        for s, raw in bars.items():
            raw = sorted(raw, key=lambda b: b.timestamp)
            days = [b.timestamp.astimezone(ET).date().isoformat() for b in raw]
            splits, dividends = acts.get(s, ([], []))
            factors = build_factors([(d, b.close) for d, b in zip(days, raw)], splits, dividends)
            out[s] = [DailyBar(s, d, b.open, b.high, b.low, b.close, b.volume, fa, fs)
                      for d, b, (fa, fs) in zip(days, raw, factors)]
        yield out


def fetch_daily(client, actions, symbols, through: date, sleep=time.sleep) -> dict[str, list[DailyBar]]:
    return {s: bars for batch in batches(client, actions, symbols, through, sleep) for s, bars in batch.items()}


def refresh_daily(client, actions, store: Store, symbols, now=None, sleep=time.sleep,
                  progress=lambda done, total: None) -> bool:
    """Download everything again unless the stored data already reaches the latest final day.
    Each symbol is replaced in one transaction; the marker is set last, so a failure retries all."""
    through = last_final_date(now or datetime.now(timezone.utc))
    if store.daily_through() == through.isoformat():
        return False
    total, got = -(-len(symbols) // MAX_SYMBOLS), set()
    for done, batch in enumerate(batches(client, actions, symbols, through, sleep), 1):
        for s, bars in batch.items():
            store.replace_daily(s, bars)
            got.add(s)
        progress(done, total)
    store.set_daily_through(through.isoformat())
    missing = sorted(set(symbols) - got)
    if missing:
        log.info("no daily bars for %d symbols: %s", len(missing), ", ".join(missing))
    return True


def load_bars(store: Store, now=None) -> pd.DataFrame:
    through = last_final_date(now or datetime.now(timezone.utc))
    df = pd.DataFrame.from_records(list(store.daily_rows(through.isoformat())), columns=COLUMNS)
    df["date"] = pd.to_datetime(df["date"])
    return df
