import os
from datetime import date, datetime, timezone
from functools import cache

import pytest
from alpaca.data.enums import CorporateActionsType
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.requests import CorporateActionsRequest

from app.daily import HISTORY_START, fetch_daily, last_final_date
from app.settings import load_keys

pytestmark = pytest.mark.skipif(os.environ.get("STOCKBARS_LIVE") != "1", reason="needs a real key")


@cache
def bars(symbol):
    keys = load_keys()
    assert keys, "no stored Alpaca key"
    through = last_final_date(datetime.now(timezone.utc))
    return {b.date: b for b in fetch_daily(StockHistoricalDataClient(*keys), CorporateActionsClient(*keys),
                                           [symbol], through)[symbol]}


def ratio(symbol, field, before, after):
    return getattr(bars(symbol)[after], field) / getattr(bars(symbol)[before], field)


def raw_close(symbol, day):
    return bars(symbol)[day].c


def spy_dividends():
    keys = load_keys()
    data = CorporateActionsClient(*keys).get_corporate_actions(CorporateActionsRequest(
        symbols=["SPY"], start=HISTORY_START, end=date.today(),
        types=[CorporateActionsType.CASH_DIVIDEND], limit=None)).data
    return {a.ex_date.isoformat(): a.rate for a in data.get("cash_dividends", [])}


def test_aapl_4_for_1_split():
    assert ratio("AAPL", "factor_split", "2020-08-28", "2020-08-31") == pytest.approx(4, rel=1e-3)


def test_tsla_3_for_1_split():
    assert ratio("TSLA", "factor_split", "2022-08-24", "2022-08-25") == pytest.approx(3, rel=1e-3)


def test_aapl_25_cent_dividend():
    prev_close = raw_close("AAPL", "2024-08-09")
    assert ratio("AAPL", "factor_all", "2024-08-09", "2024-08-12") == pytest.approx(prev_close / (prev_close - 0.25), rel=1e-4)


def test_known_spy_dividend_gaps_unchanged():
    # Alpaca's known gaps (spec, "Known data gaps"); a failure means the data changed, so update the spec
    dates = spy_dividends()
    assert "2016-03-18" not in dates and "2018-06-15" not in dates
    assert dates["2026-03-20"] == pytest.approx(1.797)
