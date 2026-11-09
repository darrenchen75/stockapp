"""Hand-computed runs: the runner is proven here independently of any real data."""
import pandas as pd
import pytest

from app.backtest import StrategyError, run
from tests.backtest_cases import D, DIVIDEND, SPLIT, all_in, bars, plain

# A has no bar on D[2]; B trades every day
bars_with_a_gap = bars([("A", D[0], 10, 10, 1, 1, 1), ("A", D[1], 10, 10, 1, 1, 1), ("A", D[3], 10, 10, 1, 1, 1),
                        ("B", D[0], 10, 10, 1, 1, 1), ("B", D[1], 10, 10, 1, 1, 1),
                        ("B", D[2], 10, 10, 1, 1, 1), ("B", D[3], 10, 10, 1, 1, 1)])
# B's first bar is D[2]
late_listing = bars([("A", d, 10, 10, 1, 1, 1) for d in D] + [("B", D[2], 10, 10, 1, 1, 1), ("B", D[3], 10, 10, 1, 1, 1)])
base = plain
# Same as plain, then a 2-for-1 split on D[3]: earlier factors halve, D[3] prices halve
with_later_split = bars([("A", D[0], 10, 10, 1, .5, .5), ("A", D[1], 10, 11, 1, .5, .5),
                         ("A", D[2], 12, 12, 1, .5, .5), ("A", D[3], 6, 7.5, 2, 1, 1)])


def switch_to_b(history, as_of):
    return {"A": 1.0} if as_of < pd.Timestamp(D[1]) else {"B": 1.0}


def raises(history, as_of):
    raise RuntimeError("boom")


def record(data):
    seen = {}

    def strategy(history, as_of):
        assert history["c"].index.max() == as_of
        seen[as_of.strftime("%Y-%m-%d")] = history
        return {"A": 1.0}

    run(strategy, data, D[0], D[3])
    return seen


def test_full_run_matches_hand_computed_values():
    r = run(all_in("A"), plain, D[0], D[3], capital=1000)
    assert r.portfolio["value"].tolist() == pytest.approx([1000, 1100, 1200, 1500], abs=1e-6)
    assert r.portfolio["return"].tolist() == pytest.approx([0, .1, 1 / 11, .25], abs=1e-6)


def test_fills_at_next_open_and_records_final_decision_with_no_fill():
    r = run(all_in("A"), plain, D[0], D[3], capital=1000)
    assert r.positions.iloc[0]["fill_price"] == 10 and r.positions.iloc[0]["shares"] == 100
    last = r.positions.iloc[-1]
    assert last["decided_on"] == pd.Timestamp(D[3]) and pd.isna(last["date"]) and last["target_weight"] == 1


def test_split_does_not_change_value_and_shares_report_actual_count():
    r = run(all_in("A"), SPLIT, D[0], D[3], capital=1000)
    assert r.portfolio["value"].tolist() == pytest.approx([1000, 1000, 1000, 1200], abs=1e-6)
    assert r.positions["shares"].tolist()[:2] == pytest.approx([10, 20])
    assert r.positions["fill_price"].iloc[0] == 100


def test_dividend_counts_only_when_setting_is_on():
    on = run(all_in("A"), DIVIDEND, D[0], D[2], capital=1000)
    off = run(all_in("A"), DIVIDEND, D[0], D[2], capital=1000, dividends=False)
    assert on.portfolio["value"].tolist() == pytest.approx([1000, 1000, 1000], abs=1e-6)
    assert off.portfolio["value"].tolist() == pytest.approx([1000, 1000, 980], abs=1e-6)


def test_later_prices_and_splits_do_not_change_earlier_decisions():
    seen_a, seen_b = record(base), record(with_later_split)
    for day in D[:3]:
        for field in "ohlcv":
            pd.testing.assert_frame_equal(seen_a[day][field], seen_b[day][field])


def test_first_fill_is_at_the_next_days_open_not_the_decision_days():
    # D[0] opens at 10, D[1] at 20: a runner that filled at its own decision day's open would pay 10
    data = bars([("A", D[0], 10, 10, 1, 1, 1), ("A", D[1], 20, 20, 1, 1, 1), ("A", D[2], 20, 20, 1, 1, 1)])
    first = run(all_in("A"), data, D[0], D[2], capital=1000).positions.iloc[0]
    assert first["fill_price"] == 20 and first["date"] == pd.Timestamp(D[1])
    assert first["shares"] == 50 and first["decided_on"] == pd.Timestamp(D[0])


def test_strategy_never_receives_bars_after_its_decision_day():
    record(plain)  # the strategy asserts history["c"].index.max() == as_of on every call


def test_blocked_sale_then_replacement_keeps_cash_non_negative():
    r = run(switch_to_b, bars_with_a_gap, D[0], D[3], capital=1000)
    assert (r.portfolio["cash"] >= 0).all() and r.portfolio["warning"].notna().any()


def test_full_target_with_costs_keeps_cash_non_negative():
    r = run(all_in("A"), plain, D[0], D[3], capital=1000, cost_bps=100)
    assert (r.portfolio["cash"] >= 0).all()
    assert r.portfolio["value"].iloc[1] == pytest.approx(1000 / 1.01 / 10 * 11, abs=1e-6)


def test_symbol_not_yet_trading_stays_in_cash_with_warning():
    r = run(all_in("B"), late_listing, D[0], D[3], capital=1000)
    assert r.portfolio["value"].iloc[1] == 1000
    assert r.positions["warning"].notna().any()


def test_held_symbol_with_no_bar_keeps_last_price_with_warning():
    r = run(all_in("A"), bars_with_a_gap, D[0], D[3], capital=1000)
    assert r.positions["warning"].notna().any()


def test_strategy_exception_names_the_date():
    with pytest.raises(StrategyError, match=D[0]):
        run(raises, plain, D[0], D[3])


@pytest.mark.parametrize("start,end", [("2030-01-01", "2030-02-01"), (D[3], D[0])])
def test_range_without_trading_days_is_a_clear_error(start, end):
    with pytest.raises(ValueError, match="no trading days"):
        run(all_in("A"), plain, start, end)
