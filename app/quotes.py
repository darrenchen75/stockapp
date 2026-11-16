"""Stats-panel figures from regular-session bars only. Missing data is None, never 0."""
import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from app.listing import Listing
from app.sessions import Session, regular
from app.store import Bar


@dataclass(frozen=True)
class Quote:
    name: str | None = None
    last: float | None = None
    chg_1d: float | None = None
    chg_5d: float | None = None
    chg_30d: float | None = None
    volume: float | None = None
    vol: float | None = None
    vol_n: int | None = None
    sector: str | None = None


def quote(listing: Listing, bars: list[Bar], sessions: list[Session], today: date) -> Quote:
    days = regular(bars, sessions)
    q = Quote(name=listing.name, sector=listing.sector)
    if not days:
        return q
    order = sorted(days)
    closes = {d: days[d][-1].c for d in order}
    last = closes[order[-1]]

    def chg(ref):
        return (last - ref) / ref * 100 if ref else None  # same form as Summary.pct

    def since(n):
        old = [d for d in order if d <= today - timedelta(days=n)]
        return chg(closes[old[-1]]) if old else None

    recent = [closes[d] for d in order if d >= today - timedelta(days=30)]
    rets = [b / a - 1 for a, b in zip(recent, recent[1:]) if a]
    vol = statistics.stdev(rets) * math.sqrt(252) if len(rets) >= 2 else None
    return Quote(listing.name, last, chg(closes[order[-2]]) if len(order) > 1 else None,
                 since(5), since(30), sum(b.v for b in days[order[-1]]),
                 vol, len(rets) if vol is not None else None, listing.sector)
