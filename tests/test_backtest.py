import pandas as pd

from app.backtest import history_as_of, prepare
from tests.backtest_cases import D, DIVIDEND, SPLIT, bars

# A has bars on D[0] and D[1] only (factors .5), B has all three days
two_symbols = bars([("A", D[0], 100, 100, 10, .5, .5), ("A", D[1], 100, 100, 10, .5, .5),
                    ("B", D[0], 10, 10, 5, 1, 1), ("B", D[1], 10, 10, 5, 1, 1), ("B", D[2], 10, 10, 5, 1, 1)])
# SPY has D[0] and D[1]; A also has D[2]
with_spy = bars([("SPY", D[0], 300, 300, 1, 1, 1), ("SPY", D[1], 300, 300, 1, 1, 1),
                 ("A", D[0], 100, 100, 10, 1, 1), ("A", D[1], 100, 100, 10, 1, 1), ("A", D[2], 100, 100, 10, 1, 1)])


def test_history_before_a_split_shows_prices_as_they_traded():
    h = history_as_of(prepare(SPLIT), pd.Timestamp(D[1]))
    assert h["c"]["A"].tolist() == [100, 100]
    assert h["v"]["A"].tolist() == [10, 10]


def test_history_after_a_split_restates_earlier_days():
    h = history_as_of(prepare(SPLIT), pd.Timestamp(D[2]))
    assert h["c"]["A"].tolist() == [50, 50, 50]
    assert h["v"]["A"].tolist() == [20, 20, 20]


def test_dividend_restates_price_but_not_volume():
    h = history_as_of(prepare(DIVIDEND), pd.Timestamp(D[2]))
    assert h["c"]["A"].tolist() == [98, 98, 98]
    assert h["v"]["A"].tolist() == [10, 10, 10]


def test_splits_only_setting_ignores_dividends():
    h = history_as_of(prepare(DIVIDEND, dividends=False), pd.Timestamp(D[2]))
    assert h["c"]["A"].tolist() == [100, 100, 98]


def test_history_uses_last_factor_when_a_symbol_has_no_bar_that_day():
    h = history_as_of(prepare(two_symbols), pd.Timestamp(D[2]))
    assert h["c"]["A"].tolist()[:2] == [100, 100] and pd.isna(h["c"]["A"].iloc[2])


def test_days_come_from_spy_when_present():
    assert prepare(with_spy).days == [pd.Timestamp(D[0]), pd.Timestamp(D[1])]


def test_changing_returned_history_does_not_change_prices():
    p = prepare(SPLIT)
    h = history_as_of(p, pd.Timestamp(D[1]))
    h["c"].iloc[0, 0] = -1
    assert history_as_of(p, pd.Timestamp(D[1]))["c"]["A"].iloc[0] == 100
