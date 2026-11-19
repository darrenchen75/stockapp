import warnings

import pandas as pd
import pytest

from app.stats import best, drawdown, growth, risk, summary


def portfolio(values, start="2020-01-01"):
    dates = pd.date_range(start, periods=len(values), freq="365D")  # one row per ~year
    return pd.DataFrame({"date": dates, "value": values})


def test_growth_scales_any_capital_to_100k():
    assert growth(portfolio([50_000, 100_000])).tolist() == [100_000, 200_000]
    assert growth(portfolio([200_000, 100_000])).tolist() == [100_000, 50_000]


def test_drawdown_from_running_high():
    assert drawdown(portfolio([100, 120, 90, 130])).tolist() == pytest.approx([0, 0, -0.25, 0])


def test_summary_formulas():
    pos = pd.DataFrame({"decided_on": ["d0", "d0", "d1", "d2"], "fill_price": [10, 11, float("nan"), 12],
                        "warning": [None, "no bar today: target not filled", None, None]})
    s = summary(portfolio([100, 121], start="2020-01-01"), pos)
    assert s["total_return"] == pytest.approx(0.21)
    assert s["cagr"] == pytest.approx(1.21 ** (365.25 / 365) - 1)
    assert s["worst_drop"] == 0 and s["rebalances"] == 2 and s["warnings"] == 1


def test_single_day_run_has_no_cagr():
    s = summary(portfolio([100]), pd.DataFrame(columns=["decided_on", "fill_price", "warning"]))
    assert s["cagr"] is None and s["total_return"] == 0
    assert best({"a": {**s, "cagr": None}, "b": {**s, "cagr": .1}})["cagr"] == "b"


def test_best_marks_return_growth_and_smallest_drop_only():
    b = best({"a": {"total_return": .5, "cagr": .1, "worst_drop": -.3, "rebalances": 9, "warnings": 0},
              "b": {"total_return": .2, "cagr": .2, "worst_drop": -.1, "rebalances": 1, "warnings": 5}})
    assert b == {"total_return": "a", "cagr": "b", "worst_drop": "b"}


def test_all_nan_warning_column_counts_zero_without_pandas_warning():
    pos = pd.DataFrame({"decided_on": ["d0", "d1"], "fill_price": [10.0, 11.0], "warning": [float("nan")] * 2})
    assert pos["warning"].dtype == "float64"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert summary(portfolio([100, 121]), pos)["warnings"] == 0


def daily(returns, start="2020-01-02"):
    """Portfolio whose daily returns are exactly `returns`, starting at 100."""
    values = [100.0]
    for r in returns:
        values.append(values[-1] * (1 + r))
    return pd.DataFrame({"date": pd.bdate_range(start, periods=len(values)), "value": values})


def test_risk_regression_by_hand():
    # x = [-.01, 0, .01], y = [0, 0, .03]: slope 1.5, intercept .01, s^2 = .00015, Sxx = .0002
    r = risk(daily([0, 0, .03]), daily([-.01, 0, .01]))
    assert r["beta"] == pytest.approx(1.5) and r["beta_se"] == pytest.approx((.00015 / .0002) ** .5)
    assert r["alpha"] == pytest.approx(.01 * 252) and r["alpha_se"] == pytest.approx((.00015 / 3) ** .5 * 252)
    assert r["days"] == 3
    assert r["volatility"] == pytest.approx(pd.Series([0, 0, .03]).std() * 252 ** .5)


def test_benchmark_against_itself_is_beta_one_alpha_zero():
    spy = daily([.01, -.02, .03, .005])
    r = risk(spy, spy)
    assert r["beta"] == pytest.approx(1) and r["alpha"] == pytest.approx(0, abs=1e-12)
    assert r["beta_se"] == pytest.approx(0, abs=1e-12)


def test_risk_uses_only_dates_both_have():
    run = daily([0, 0, .03], start="2020-01-03")  # starts a day later than SPY
    spy = daily([.02, -.01, 0, .01, .05])  # SPY's extra first and last days must be ignored
    r = risk(run, spy)
    assert r["days"] == 3 and r["beta"] == pytest.approx(1.5)


def test_risk_without_benchmark_or_enough_days():
    r = risk(daily([.01, .02, .03]), None)
    assert r["beta"] is None and r["alpha"] is None and r["volatility"] is not None and r["days"] == 3
    short = risk(daily([.01, .02]), daily([.01, .03]))  # 2 returns: no degrees of freedom for the error
    assert short["beta"] is None and short["volatility"] is not None
    one = risk(daily([.01]), None)
    assert one["volatility"] is None and one["days"] == 1
