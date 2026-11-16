import logging
from bisect import bisect_left
from functools import lru_cache
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import numpy as np

from app.store import ET, Bar, Store

log = logging.getLogger(__name__)

ANCHOR, DAY_MIN = date(2000, 1, 3), 960  # extended day 04:00-20:00 ET, in minutes
_warned = False


@dataclass(frozen=True)
class Session:
    day: date
    open: int   # Unix seconds
    close: int


def _at(day: date, h: int, m: int) -> int:
    return int(datetime.combine(day, time(h, m), ET).timestamp())


def fallback(days: Iterable[date]) -> list[Session]:
    return [Session(d, _at(d, 9, 30), _at(d, 16, 0)) for d in sorted(set(days)) if d.weekday() < 5]


def load(store: Store) -> list[Session]:
    """Cached sessions plus 09:30-16:00 fallbacks for bar days outside the cached range."""
    global _warned
    rows = {s.day: s for s in store.sessions()}
    lo, hi = store.meta("calendar_from"), store.meta("calendar_to")
    last = store.latest()
    if last is not None:
        end = datetime.fromtimestamp(last, ET).date()
        days = (end - timedelta(days=36) + timedelta(days=i) for i in range(37))
        uncovered = [d for d in days if d.weekday() < 5 and d not in rows
                     and not (lo and hi and lo <= d.isoformat() <= hi)
                     and store.db.execute("SELECT 1 FROM bars WHERE ts >= ? AND ts < ? LIMIT 1",
                                          (_at(d, 0, 0), _at(d + timedelta(days=1), 0, 0))).fetchone()]
        if uncovered:
            if not _warned:
                log.warning("market calendar missing for %d day(s); assuming 09:30-16:00 ET", len(uncovered))
                _warned = True
            rows.update({s.day: s for s in fallback(uncovered)})
    return [rows[d] for d in sorted(rows)]


def holidays(sessions: list[Session]) -> list[date]:
    if not sessions:
        return []
    have = {s.day for s in sessions}
    first, last = min(have), max(have)
    return [d for i in range((last - first).days + 1)
            if (d := first + timedelta(days=i)).weekday() < 5 and d not in have]


def regular(bars: list[Bar], sessions: list[Session]) -> dict[date, list[Bar]]:
    """Bars must be in time order; each session is a bisect slice."""
    times = [b.ts for b in bars]
    out = {}
    for s in sessions:
        got = bars[bisect_left(times, s.open):bisect_left(times, s.close)]
        if got:
            out[s.day] = got
    return out


def _hol(holidays) -> np.ndarray:
    return np.array(list(holidays), dtype="datetime64[D]")


@lru_cache(maxsize=4096)
def _days(day: date, holidays: tuple) -> float:
    return float(np.busday_count(ANCHOR, day, holidays=_hol(holidays)))


def to_x(ts: int, holidays: list[date]) -> float | None:
    t = datetime.fromtimestamp(ts, ET)
    m = (t.hour - 4) * 60 + t.minute + t.second / 60
    if t.weekday() >= 5 or t.date() in holidays or not 0 <= m < DAY_MIN:
        return None
    return _days(t.date(), tuple(holidays)) * DAY_MIN + m


def from_x(x: float, holidays: list[date]) -> int:
    n, m = divmod(x, DAY_MIN)
    day = np.busday_offset(ANCHOR, int(n), holidays=_hol(holidays)).astype(object)
    return int((datetime.combine(day, time(4), ET) + timedelta(minutes=m)).timestamp())
