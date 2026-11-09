from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app import backtest
from app.backtest import (StrategyError, history_as_of, load_strategy, main, prepare, rebalance, run,
                          validate_weights, write_results)
from tests.backtest_cases import D, DIVIDEND, SPLIT, all_in, bars, plain
from tests.test_daily import FakeActions, FakeClient, fake_day
from tests.test_feed import HTTPError

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


def test_load_strategy_runs_the_dummy():
    fn = load_strategy("tests/fixtures/buy_spy.py")
    assert fn({}, None) == {"SPY": 1.0}


def test_load_strategy_without_function_is_a_clear_error(tmp_path):
    path = tmp_path / "s.py"
    path.write_text("x = 1\n")
    with pytest.raises(StrategyError, match="target_weights"):
        load_strategy(str(path))


def test_results_carry_the_required_note(tmp_path):
    write_results(run(all_in("A"), plain, D[0], D[3]), str(tmp_path), "Alpaca SIP daily")
    for name in ("portfolio.csv", "positions.csv"):
        first = (tmp_path / name).read_text().splitlines()[0]
        assert first.startswith("#") and "survivorship bias" in first
        assert "Alpaca SIP daily" in first and "Dividends: on" in first
    assert len(pd.read_csv(tmp_path / "portfolio.csv", comment="#")) == 4


@pytest.fixture
def command(tmp_path, monkeypatch):
    """Patch the key loader, db path, universe and both clients; returns the args and the keys to set."""
    from app import settings
    monkeypatch.setattr(settings, "load_keys", lambda: ("k", "s"))
    monkeypatch.setattr(backtest, "default_db_path", lambda: str(tmp_path / "bars.db"))
    monkeypatch.setattr(backtest, "universe", lambda: ["SPY"])
    monkeypatch.setattr(backtest, "CorporateActionsClient", lambda *keys: FakeActions())
    monkeypatch.setattr(backtest, "StockHistoricalDataClient",
                        lambda *keys: FakeClient({"SPY": [fake_day(d) for d in D]}))
    return ["tests/fixtures/buy_spy.py", "--start", D[0], "--end", D[3], "--out", str(tmp_path / "out")]


def test_main_runs_end_to_end(command, tmp_path):
    assert main(command) == 0
    assert (tmp_path / "out" / "portfolio.csv").exists()


def test_main_without_a_key_explains_what_to_do(command, monkeypatch, capsys):
    from app import settings
    monkeypatch.setattr(settings, "load_keys", lambda: None)
    assert main(command) == 1
    assert "Alpaca key" in capsys.readouterr().err


def test_main_reports_a_failed_download(command, monkeypatch, capsys):
    monkeypatch.setattr(backtest, "StockHistoricalDataClient", lambda *keys: FakeClient(errors=[HTTPError(403)]))
    assert main(command) == 1
    assert "Could not download" in capsys.readouterr().err


def test_viewer_does_not_import_the_backtest():
    for name in ("app/ui.py", "app/main.py"):
        text = Path(name).read_text()
        assert "app.backtest" not in text and "app.daily" not in text
