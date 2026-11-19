import logging
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.sessions import Session

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)


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
        for table in ("bars", "bars_staging"):  # staging holds SIP bars until the migration swaps them in
            self.db.execute(
                f"CREATE TABLE IF NOT EXISTS {table}("
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
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sessions("
            "day TEXT PRIMARY KEY, open INTEGER, close INTEGER) WITHOUT ROWID"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS exchanges(symbol TEXT PRIMARY KEY, exchange TEXT) WITHOUT ROWID")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID")
        self.db.execute("CREATE TABLE IF NOT EXISTS daily_meta(through TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS daily_symbols(symbol TEXT PRIMARY KEY) WITHOUT ROWID")

    def upsert(self, bars: Iterable[Bar], table: str = "bars") -> None:
        with self.db:
            self.db.executemany(
                f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?)",
                [(b.symbol, b.ts, b.o, b.h, b.l, b.c, b.v) for b in bars],
            )

    def upsert_staged(self, bars: Iterable[Bar]) -> None:
        self.upsert(bars, "bars_staging")

    def staged_latest_all(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT symbol, MAX(ts) FROM bars_staging GROUP BY symbol"))

    def swap_staged(self) -> None:
        """Replace `bars` with the staged rows and mark the feed SIP, in one transaction. Splits are
        forgotten so repair_splits re-checks every split against the new rows."""
        with self.db:  # sqlite3 opens the transaction at the first DELETE; any error rolls all back
            self.db.execute("DELETE FROM bars")
            self.db.execute("INSERT INTO bars SELECT * FROM bars_staging")
            self.db.execute("DELETE FROM bars_staging")
            self.db.execute("DELETE FROM splits")
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("feed", "sip"))
        try:  # the swap rewrote every row, so the WAL is now about as big as bars.db; hand the space back
            if self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                log.warning("WAL checkpoint after the SIP swap was blocked by a reader; it shrinks later")
        except sqlite3.Error as e:
            log.warning("WAL checkpoint after the SIP swap failed: %s", e)

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

    def daily_last(self, symbol: str) -> str | None:
        return self.db.execute("SELECT MAX(date) FROM daily_bars WHERE symbol = ?", (symbol,)).fetchone()[0]

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

    def save_sessions(self, rows) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO sessions VALUES (?,?,?)",
                                [(r.day.isoformat(), r.open, r.close) for r in rows])

    def sessions(self) -> list:
        from app.sessions import Session  # sessions imports this module
        return [Session(date.fromisoformat(d), o, c)
                for d, o, c in self.db.execute("SELECT day, open, close FROM sessions ORDER BY day")]

    def save_exchanges(self, d: dict[str, str]) -> None:
        with self.db:
            self.db.execute("DELETE FROM exchanges")
            self.db.executemany("INSERT INTO exchanges VALUES (?,?)", d.items())

    def exchanges(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT symbol, exchange FROM exchanges"))

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def handled_splits(self) -> set[tuple[str, str]]:
        return set(self.db.execute("SELECT symbol, ex_date FROM splits"))

    def latest_all(self) -> dict[str, int]:
        """Skip-scan: one primary-key seek to find each next symbol and one for its newest bar, so the
        cost grows with symbols, not rows (GROUP BY or MAX(ts) alone read every row: ~0.2s at SIP size)."""
        return dict(self.db.execute(
            "WITH RECURSIVE s(sym) AS (SELECT MIN(symbol) FROM bars UNION ALL"
            " SELECT (SELECT MIN(symbol) FROM bars WHERE symbol > sym) FROM s WHERE sym IS NOT NULL)"
            " SELECT sym, (SELECT MAX(ts) FROM bars WHERE symbol = sym) FROM s WHERE sym IS NOT NULL"))

    def latest(self) -> int | None:
        return max(self.latest_all().values(), default=None)

    def bars(self, symbol: str, since: int) -> list[Bar]:
        rows = self.db.execute(
            "SELECT * FROM bars WHERE symbol = ? AND ts >= ? ORDER BY ts", (symbol, since)
        )
        return [Bar(*r) for r in rows]

    def prune(self, days: int, now: float | None = None) -> int:
        cutoff = int(time.time() if now is None else now) - days * 86400
        with self.db:
            return self.db.execute("DELETE FROM bars WHERE ts < ?", (cutoff,)).rowcount

    def session_start(self, symbol: str | None = None) -> int | None:
        """Midnight New York time on the date of the newest bar (of `symbol`, if given)."""
        ts = self.latest() if symbol is None else self.db.execute(
            "SELECT MAX(ts) FROM bars WHERE symbol = ?", (symbol,)
        ).fetchone()[0]
        return None if ts is None else day_start(ts)

    def summary(self, sessions: "list[Session]") -> dict[str, Summary]:
        """Regular-session figures: latest session with a regular bar vs. the one before it.
        Primary-key range probes, newest session first, stopping at the second hit."""
        out = {}
        self.db.execute("BEGIN")  # one snapshot, so a split repair committing mid-read can't pull rows away
        try:
            for s in self.latest_all():
                last = volume = prev = None
                for x in sorted(sessions, key=lambda x: x.day, reverse=True):
                    row = self.db.execute(
                        "SELECT c, (SELECT SUM(v) FROM bars WHERE symbol = ? AND ts >= ? AND ts < ?)"
                        " FROM bars WHERE symbol = ? AND ts >= ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                        (s, x.open, x.close, s, x.open, x.close)).fetchone()
                    if row is None:
                        continue
                    if last is None:
                        last, volume = row
                    else:
                        prev = row[0]
                        break
                if last is not None:
                    out[s] = Summary(last, prev, volume)
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
