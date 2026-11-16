import csv

from app import daily, listing
from app.backtest import selftest
from app.feed import load_symbols
from app.listing import ETFS, load_listings, viewer_symbols


def test_load_symbols_and_backtest_universe_unchanged():
    assert load_symbols() == [r["Symbol"] for r in csv.DictReader(open("app/sp500.csv"))]
    assert daily.universe() == load_symbols() + ["SPY"]


def test_viewer_symbols_union_without_duplicates():
    v = viewer_symbols()
    assert len(v) == len(set(v)) and set(load_symbols()) <= set(v) and set(ETFS) <= set(v)
    assert {"AAPL"} <= {s for s, l in load_listings().items() if l.indexes == {"S&P 500", "Nasdaq-100", "Dow 30"}}


def test_every_listing_has_name_and_sector():
    assert all(l.name and l.sector for l in load_listings().values())


def test_etfs():
    assert set(ETFS) == set("SPY QQQ DIA XLK XLF XLV XLY XLP XLE XLI XLB XLU XLRE XLC".split())
    assert load_listings()["XLK"].etf_for == "Information Technology" and load_listings()["XLK"].sector == "ETF"
    assert len(csv.DictReader(open("app/dow30.csv")).__next__()) == 3
    assert sum("Dow 30" in l.indexes for l in load_listings().values()) == 30


def test_selftest_loads_viewer_listings(monkeypatch):
    monkeypatch.setattr(listing, "load_listings", lambda: (_ for _ in ()).throw(FileNotFoundError("nasdaq100.csv")))
    assert selftest("packaging/smoke_strategy.py") == 1
