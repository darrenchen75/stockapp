import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
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
class DailyBar:
    symbol: str
    date: str  # YYYY-MM-DD, New York date
    o: float  # o, h, l, c, v are raw (not split-adjusted)
    h: float
    l: float
    c: float
    v: float
    factor_all: float
    factor_split: float


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
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS daily_bars("
            "symbol TEXT NOT NULL, date TEXT NOT NULL,"
            "o REAL, h REAL, l REAL, c REAL, v REAL, factor_all REAL, factor_split REAL,"
            "PRIMARY KEY(symbol, date)) WITHOUT ROWID"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS daily_meta(through TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS daily_symbols(symbol TEXT PRIMARY KEY) WITHOUT ROWID")

    def upsert(self, bars: Iterable[Bar]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)",
                [(b.symbol, b.ts, b.o, b.h, b.l, b.c, b.v) for b in bars],
            )

    def replace(self, symbol: str, bars: Iterable[Bar], ex_date: str | None) -> None:
        """Swap all of `symbol`'s rows for split-adjusted `bars` and mark the split handled (unless
        `ex_date` is None), atomically. Deleting first means no raw-priced minute survives."""
        with self.db:
            self.db.execute("DELETE FROM bars WHERE symbol = ?", (symbol,))
            self.db.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)",
                [(b.symbol, b.ts, b.o, b.h, b.l, b.c, b.v) for b in bars],
            )
            if ex_date:
                self.db.execute("INSERT OR IGNORE INTO splits VALUES (?,?)", (symbol, ex_date))

    def replace_daily(self, symbol: str, bars: Iterable[DailyBar]) -> None:
        """Swap all of `symbol`'s daily rows, atomically. Plain INSERT so bad data raises and rolls back."""
        with self.db:
            self.db.execute("DELETE FROM daily_bars WHERE symbol = ?", (symbol,))
            self.db.executemany(
                "INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?)",
                [(b.symbol, b.date, b.o, b.h, b.l, b.c, b.v, b.factor_all, b.factor_split) for b in bars],
            )

    def daily_rows(self, through: str, symbols: Iterable[str] | None = None) -> Iterable[tuple]:
        """Rows through `through`, or only those of `symbols` if given."""
        sql, args = "SELECT * FROM daily_bars WHERE date <= ?", [through]
        if symbols is not None:
            symbols = list(symbols)
            sql += f" AND symbol IN ({','.join('?' * len(symbols))})"
            args += symbols
        return self.db.execute(sql + " ORDER BY date, symbol", args)

    def daily_through(self) -> str | None:
        row = self.db.execute("SELECT through FROM daily_meta").fetchone()
        return row[0] if row else None

    def set_daily_through(self, date: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM daily_meta")
            self.db.execute("INSERT INTO daily_meta VALUES (?)", (date,))

    def daily_symbols(self) -> set[str]:
        """The symbols the last finished daily download asked for."""
        return {r[0] for r in self.db.execute("SELECT symbol FROM daily_symbols")}

    def set_daily_symbols(self, symbols: Iterable[str]) -> None:
        with self.db:
            self.db.execute("DELETE FROM daily_symbols")
            self.db.executemany("INSERT INTO daily_symbols VALUES (?)", [(s,) for s in set(symbols)])

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
        self.db.execute("BEGIN")  # one snapshot, so a split repair committing mid-read can't pull rows away
        try:
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
        finally:
            self.db.commit()
        return out

    def gaps(self, symbols: Iterable[str], days: int = 30, now: float | None = None) -> dict[str, int]:
        """Trading days (days any symbol has bars) on which each symbol has none: fetch holes, halts,
        stale feeds. Counted by day, not minute, because IEX-only data has many empty minutes.
        The oldest day is skipped since prune cuts it mid-day, and today since sparse symbols may
        not have traded yet. Symbols with no rows are left out."""
        today = datetime.fromtimestamp(time.time() if now is None else now, ET).date()
        starts = [int(datetime.combine(today - timedelta(days=i), clock(), ET).timestamp())
                  for i in range(days, -1, -1)]  # today's midnight closes yesterday's range
        have = {s: {a for a, b in zip(starts, starts[1:]) if self.db.execute(
            "SELECT 1 FROM bars WHERE symbol = ? AND ts >= ? AND ts < ? LIMIT 1", (s, a, b)
        ).fetchone()} for s in symbols}  # primary-key probes: ~0.1s for 500 symbols, vs ~1s for a scan
        sessions = set(sorted(set().union(*have.values()))[1:])
        return {s: len(sessions - got) for s, got in have.items() if got}
