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
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS splits("
            "symbol TEXT NOT NULL, ex_date TEXT NOT NULL,"
            "PRIMARY KEY(symbol, ex_date)) WITHOUT ROWID"
        )

    def upsert(self, bars: Iterable[Bar]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)",
                [(b.symbol, b.ts, b.o, b.h, b.l, b.c, b.v) for b in bars],
            )

    def replace(self, symbol: str, bars: Iterable[Bar], ex_date: str) -> None:
        """Swap all of `symbol`'s rows for split-adjusted `bars` and mark the split handled, atomically.
        Deleting first means no raw-priced minute survives next to adjusted ones."""
        with self.db:
            self.db.execute("DELETE FROM bars WHERE symbol = ?", (symbol,))
            self.db.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)",
                [(b.symbol, b.ts, b.o, b.h, b.l, b.c, b.v) for b in bars],
            )
            self.db.execute("INSERT OR IGNORE INTO splits VALUES (?,?)", (symbol, ex_date))

    def handled_splits(self) -> set[tuple[str, str]]:
        return set(self.db.execute("SELECT symbol, ex_date FROM splits"))

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
