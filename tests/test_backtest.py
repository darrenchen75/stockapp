import numpy as np
import pandas as pd
import pytest

from app.backtest import StrategyError, history_as_of, prepare, rebalance, validate_weights
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


def test_sales_pay_for_purchases():
    f = rebalance({"A": 10}, 0, {"B": 1}, {"A": 10, "B": 5}, {"A": 10, "B": 5})
    assert f.shares["B"] == 20 and f.shares.get("A", 0) == 0 and f.cash == 0 and not f.scaled


def test_blocked_sale_cannot_fund_a_purchase():
    f = rebalance({"A": 10}, 0, {"B": 1}, {"B": 5}, {"A": 10, "B": 5})
    assert f.shares["A"] == 10 and f.shares.get("B", 0) == 0 and f.cash == 0 and f.scaled


def test_blocked_sale_limits_purchase_to_available_cash():
    f = rebalance({"A": 10}, 100, {"B": 1}, {"B": 5}, {"A": 10, "B": 5})
    assert f.shares["A"] == 10 and f.shares["B"] == 20 and f.cash == 0 and f.scaled


def test_full_target_with_costs_scales_to_zero_cash():
    f = rebalance({}, 1000, {"A": 1}, {"A": 10}, {}, cost_bps=100)
    assert f.shares["A"] == pytest.approx(1000 / 1.01 / 10) and f.cash == pytest.approx(0) and f.cash >= 0 and f.scaled


def test_sale_pays_its_cost():
    f = rebalance({"A": 10}, 0, {}, {"A": 10}, {"A": 10}, cost_bps=100)
    assert f.cash == pytest.approx(99) and f.shares.get("A", 0) == 0


def test_zero_cost_full_target_needs_no_scaling():
    f = rebalance({"A": 10}, 0, {"A": .5, "B": .5}, {"A": 10, "B": 5}, {"A": 10, "B": 5})
    assert f.shares["A"] == pytest.approx(5) and f.shares["B"] == pytest.approx(10)
    assert f.cash == pytest.approx(0) and f.cash >= 0 and not f.scaled


def test_target_without_an_open_stays_in_cash():
    f = rebalance({}, 100, {"B": 1}, {}, {})
    assert f.unfilled == ["B"] and f.cash == 100 and f.shares.get("B", 0) == 0


@pytest.mark.parametrize("weights", [{"ZZZ": .5}, {"A": -.1}, {"A": 1.1}, {"A": .8, "B": .7},
                                     {"A": float("nan")}, None])
def test_invalid_weights_stop_the_run_naming_the_date(weights):
    with pytest.raises(StrategyError, match="2020-01-02"):
        validate_weights(weights, {"A", "B"}, pd.Timestamp("2020-01-02"))


def test_valid_weights_are_accepted():
    assert validate_weights({}, {"A"}, "2020-01-02") == {}
    assert validate_weights({"A": np.float64(.4), "B": np.float32(.5)}, {"A", "B"}, "2020-01-02")["A"] == .4
    assert validate_weights({"A": .5, "B": .5 + 1e-10}, {"A", "B"}, "2020-01-02")
