import math
from datetime import date, datetime, timezone

import pytest

from app.paper_plan import Limits, check_losses, order_id, plan_orders, run_window
from app.store import ET

THU, FRI, MON = date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9)
CAL = [THU, FRI, MON]


def at(y, m, d, h, mi, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=ET)


def test_window_opens_at_2030_and_closes_at_0928():
    t, n, why = run_window(CAL, at(2026, 11, 6, 20, 29, 59))
    assert t == THU and why
    assert run_window(CAL, at(2026, 11, 6, 20, 30)) == (FRI, MON, None)
    assert run_window(CAL, at(2026, 11, 7, 12, 0)) == (FRI, MON, None)
    assert run_window(CAL, at(2026, 11, 9, 9, 27, 59)) == (FRI, MON, None)
    assert run_window(CAL, at(2026, 11, 9, 9, 28))[2]


def test_window_skips_holiday():
    cal = [date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 27)]
    assert run_window(cal, at(2026, 11, 25, 21, 0)) == (date(2026, 11, 25), date(2026, 11, 27), None)


def test_window_across_dst_change():
    # UTC-aware now: zoneinfo-vs-zoneinfo comparison would ignore the offset. DST ends 2026-11-01.
    cal = [date(2026, 10, 30), date(2026, 11, 2)]
    fri, mon = cal
    assert run_window(cal, datetime(2026, 10, 31, 0, 30, tzinfo=timezone.utc)) == (fri, mon, None)  # Fri 20:30 EDT
    assert run_window(cal, datetime(2026, 11, 2, 14, 27, tzinfo=timezone.utc)) == (fri, mon, None)  # Mon 09:27 EST
    assert run_window(cal, datetime(2026, 11, 2, 14, 28, tzinfo=timezone.utc))[2]  # Mon 09:28 EST


def test_window_closed_before_first_session_or_after_last():
    assert run_window(CAL, at(2026, 11, 5, 12, 0))[2]
    assert run_window(CAL, at(2026, 11, 9, 21, 0))[2]


def test_order_id_format_and_length():
    assert order_id("PA3ABCDEFGH", date(2026, 11, 6), 2, "BRK.B") == "sb-PA3ABCDEFGH-20261106-a2-BRK.B"
    with pytest.raises(ValueError):
        order_id("PA3ABCDEFGHIJKLMNOPQRSTUVWXYZ", date(2026, 11, 6), 2, "BRK.B")


def plan(targets, positions=None, closes=None, equity=100_000, locked=False, splits=frozenset(), limits=Limits()):
    return plan_orders(targets, positions or {}, equity, {s: 10.0 for s in targets} if closes is None else closes, limits, locked,
                       set(splits), "PA1", FRI, 1)


def test_sizing_uses_buffer_and_floor():
    orders, _ = plan({"A": 0.5}, closes={"A": 33.0}, limits=Limits(max_weight=1.0))
    assert [(o.side, o.qty, o.ref_price) for o in orders] == [("buy", 1484, 33.0)]


def test_cap_trims_buys_only():
    orders, notes = plan({"A": 0.6})
    assert [(o.side, o.qty) for o in orders] == [("buy", 2500)]
    assert [n.kind for n in notes] == ["trim_cap"]
    orders, _ = plan({"A": 0.6}, positions={"A": 3000})
    assert orders == []


def test_untargeted_goes_to_zero_sales_first():
    orders, _ = plan({"A": 0.2}, positions={"B": 10}, closes={"A": 10.0, "B": 5.0})
    assert [(o.symbol, o.side, o.qty) for o in orders] == [("B", "sell", 10), ("A", "buy", 1960)]


def test_partial_sell_toward_smaller_target():
    orders, _ = plan({"A": 0.1}, positions={"A": 2000})
    assert [(o.side, o.qty) for o in orders] == [("sell", 1020)]


def test_loss_lock_drops_buys_keeps_sales():
    orders, notes = plan({"A": 0.2}, positions={"B": 10}, closes={"A": 10.0, "B": 5.0}, locked=True)
    assert [(o.symbol, o.side) for o in orders] == [("B", "sell")]
    assert [n.kind for n in notes] == ["blocked_loss"]


def test_split_symbol_blocked():
    orders, notes = plan({"A": 0.2}, positions={"B": 10}, closes={"A": 10.0, "B": 5.0}, splits={"A", "B"})
    assert orders == [] and [n.kind for n in notes] == ["blocked_split"] * 2


def test_held_symbol_without_close_is_sold():
    orders, notes = plan({}, positions={"B": 10})
    assert orders[0].side == "sell" and orders[0].qty == 10 and math.isnan(orders[0].ref_price)
    assert [n.kind for n in notes] == ["no_close"]


def test_targeted_symbol_without_close_is_neither_bought_nor_sold():
    orders, notes = plan({"A": 0.2}, positions={"A": 10}, closes={})
    assert orders == [] and [n.kind for n in notes] == ["no_close"]
    orders, notes = plan({"A": 0.2}, closes={"A": math.nan})
    assert orders == [] and [n.kind for n in notes] == ["no_close"]


def test_fractional_holding_trades_whole_shares():
    orders, notes = plan({}, positions={"A": 10.5}, closes={"A": 10.0})
    assert [(o.side, o.qty) for o in orders] == [("sell", 10)]
    assert [n.kind for n in notes] == ["fractional"]


MON_, TUE, WED, THU_ = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)


def test_losses_checks_every_session_after_last():
    r = check_losses([(MON_, 100.0), (TUE, 92.0), (WED, 93.0)], MON_, 0.03)
    assert [d for d, _ in r.breaches] == [TUE] and r.breaches[0][1] == pytest.approx(-0.08)
    assert r.checked_through == WED and r.problem is None


def test_losses_missing_or_zero_history_is_a_problem():
    assert check_losses([], None, 0.03).problem
    assert check_losses([], MON_, 0.03).checked_through is None
    assert check_losses([(MON_, 100.0)], TUE, 0.03).problem
    assert check_losses([(MON_, 0.0), (TUE, 100.0)], MON_, 0.03).problem
    assert check_losses([(MON_, 100.0), (TUE, 0.0)], MON_, 0.03).problem


def test_losses_first_run_checks_only_latest_session():
    h = [(MON_, 0.0), (TUE, 0.0), (WED, 100_000.0), (THU_, 99_000.0)]
    r = check_losses(h, None, 0.03)
    assert r.breaches == [] and r.problem is None and r.checked_through == THU_
    r = check_losses(h[:3] + [(THU_, 90_000.0)], None, 0.03)
    assert r.breaches[0][0] == THU_ and r.breaches[0][1] == pytest.approx(-0.10)


def test_losses_first_run_after_funding_day_is_not_a_loss():
    r = check_losses([(MON_, 0.0), (TUE, 100_000.0)], None, 0.03)
    assert r.breaches == [] and r.problem is None and r.checked_through == TUE
    assert check_losses([(MON_, 100.0), (TUE, 0.0)], None, 0.03).problem


def test_losses_keep_checking_past_a_zero_equity():
    r = check_losses([(MON_, 100.0), (TUE, 0.0), (WED, 90.0), (THU_, 80.0)], MON_, 0.03)
    assert r.problem and str(TUE) in r.problem and r.checked_through == THU_
    assert dict(r.breaches)[THU_] == pytest.approx(80 / 90 - 1)  # Wed to Thu, about -11%
