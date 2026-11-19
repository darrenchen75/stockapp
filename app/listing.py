"""What the viewer lists: S&P 500, Nasdaq-100, Dow 30 and chart ETFs. Backtests and paper use feed.load_symbols() only."""
import csv
from dataclasses import dataclass
from pathlib import Path


HERE = Path(__file__).parent
SP, NDX, DOW = "S&P 500", "Nasdaq-100", "Dow 30"
ETFS = {
    "XLK": ("Technology Select Sector SPDR", "Information Technology"),
    "XLF": ("Financial Select Sector SPDR", "Financials"),
    "XLV": ("Health Care Select Sector SPDR", "Health Care"),
    "XLY": ("Consumer Discretionary Select Sector SPDR", "Consumer Discretionary"),
    "XLP": ("Consumer Staples Select Sector SPDR", "Consumer Staples"),
    "XLE": ("Energy Select Sector SPDR", "Energy"),
    "XLI": ("Industrial Select Sector SPDR", "Industrials"),
    "XLB": ("Materials Select Sector SPDR", "Materials"),
    "XLU": ("Utilities Select Sector SPDR", "Utilities"),
    "XLRE": ("Real Estate Select Sector SPDR", "Real Estate"),
    "XLC": ("Communication Services Select Sector SPDR", "Communication Services"),
    "SPY": ("SPDR S&P 500 ETF Trust", SP),
    "QQQ": ("Invesco QQQ Trust", NDX),
    "DIA": ("SPDR Dow Jones Industrial Average ETF Trust", DOW),
}


@dataclass(frozen=True)
class Listing:
    symbol: str
    name: str
    sector: str
    indexes: frozenset[str]
    etf_for: str | None = None


def load_listings() -> dict[str, Listing]:
    out: dict[str, Listing] = {}
    for index, path, name_col in ((SP, "sp500.csv", "Security"), (NDX, "nasdaq100.csv", "Name"),
                                  (DOW, "dow30.csv", "Name")):
        with open(HERE / path, newline="") as f:
            for r in csv.DictReader(f):
                old = out.get(r["Symbol"])
                out[r["Symbol"]] = Listing(r["Symbol"], old.name if old else r[name_col],
                                           old.sector if old else r["GICS Sector"],
                                           (old.indexes if old else frozenset()) | {index})
    for s, (name, group) in ETFS.items():
        out[s] = Listing(s, name, "ETF", frozenset(), group)
    return out


def viewer_symbols() -> list[str]:
    return sorted(load_listings())
