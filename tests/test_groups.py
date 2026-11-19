from app.groups import BY, build
from app.listing import load_listings

L = load_listings()


def test_index_groups_hold_stock_in_each_index():
    g = {x.name: x for x in build("index", L, {}, {"AAPL": 1.0, "MSFT": 3.0, "QQQ": 9.0})}
    assert "AAPL" in g["Dow 30"].members and "AAPL" in g["Nasdaq-100"].members
    assert "QQQ" not in g["Nasdaq-100"].members and g["Nasdaq-100"].etf == "QQQ"
    assert g["S&P 500"].etf == "SPY" and g["Dow 30"].etf == "DIA"


def test_average_skips_blanks_and_none_when_all_blank():
    g = build("sector", L, {}, {"AAPL": 1.0, "MSFT": None})
    assert next(x for x in g if x.name == "Information Technology").avg_pct == 1.0
    assert next(x for x in g if x.name == "Energy").avg_pct is None


def test_sector_groups_sorted_without_etfs():
    g = build("sector", L, {}, {})
    assert [x.name for x in g] == sorted(x.name for x in g) and "ETF" not in [x.name for x in g]
    tech = next(x for x in g if x.name == "Information Technology")
    assert tech.etf == "XLK" and "XLK" not in tech.members and "AAPL" in tech.members


def test_unknown_exchange_group():
    g = {x.name: x for x in build("exchange", L, {"AAPL": "NASDAQ"}, {"AAPL": 2.0, "MSFT": 4.0})}
    assert g["NASDAQ"].members == ["AAPL"] and g["NASDAQ"].avg_pct == 2.0 and g["NASDAQ"].etf is None
    assert "MSFT" in g["Unknown"].members and "SPY" not in g["Unknown"].members and g["Unknown"].etf is None


def test_none_is_flat():
    assert BY == ("none", "sector", "exchange", "index") and build("none", L, {}, {}) == []
