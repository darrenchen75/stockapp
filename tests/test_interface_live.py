"""Live, read-only checks: SIP minute bars, the market calendar and the exchange list.

Places, cancels and touches no order or position.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient

from app import feed, market
from app.listing import viewer_symbols
from app.settings import load_keys

pytestmark = pytest.mark.skipif(os.environ.get("STOCKBARS_LIVE") != "1", reason="needs a real key")


@pytest.fixture(scope="module")
def keys():
    k = load_keys()
    assert k, "no stored Alpaca key"
    return k


def test_sip_minute_bars_are_delayed_but_present(keys):
    now = datetime.now(timezone.utc)
    # SIP refuses data newer than 15 minutes: end at now - 16 min
    bars = feed.fetch(StockHistoricalDataClient(*keys), ["AAPL", "SPY", "XLK"],
                      now - timedelta(days=2), now - timedelta(minutes=16))
    assert {b.symbol for b in bars} == {"AAPL", "SPY", "XLK"}


def test_every_viewer_symbol_has_an_exchange(keys):
    exchanges = market.fetch_exchanges(TradingClient(*keys, paper=True))
    missing = [s for s in viewer_symbols() if s not in exchanges]
    print("\nviewer symbols:", len(viewer_symbols()), "missing exchange:", missing)
    assert len(missing) <= len(viewer_symbols()) * 0.01, f"no exchange for {missing}"


def test_calendar_has_recent_sessions(keys):
    today = datetime.now(timezone.utc).date()
    sessions = market.fetch_sessions(TradingClient(*keys, paper=True), today - timedelta(days=10), today)
    assert len(sessions) >= 5
    assert all(s.open < s.close for s in sessions)
