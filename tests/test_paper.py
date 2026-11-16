import hashlib
import json
import math
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce

from app import backtest, paper, paper_state
from app.daily import load_bars
from app.paper import BrokerOrder, Refusal
from app.paper_plan import Order
from app.paper_state import AccountStore, Busy
from app.store import ET, DailyBar, Store
from tests.paper_fakes import DUPLICATE, FakeBroker, rejection
from tests.test_daily import FakeActions, FakeClient, fake_day

T, T1 = date(2026, 11, 12), date(2026, 11, 13)
NOW = datetime(2026, 11, 12, 21, 0, tzinfo=ET)
SESSIONS = [date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20)]
BAR_DAYS = [d for d in SESSIONS if d <= T]
SYMBOLS = ["AAPL", "SPY"]
CLOSES = {"SPY": 100.0, "AAPL": 50.0}
NUMBER = "PA0000001"
BUY_SPY = Path(__file__).parent / "fixtures" / "buy_spy.py"
FIFTHS = "def target_weights(history, as_of):\n    return {'SPY': 0.2, 'AAPL': 0.2}\n"


def bars(symbol, through=T):
    c = CLOSES[symbol]
    return [DailyBar(symbol, d.isoformat(), c, c, c, c, 1000.0, 1.0, 1.0) for d in BAR_DAYS if d <= through]


def order(cid, status):
    return BrokerOrder(cid, "x", cid.rsplit("-", 1)[1], "buy", 1.0, 0.0, None, status)


def oid(attempt, symbol, day=T):
    return f"sb-{NUMBER}-{day:%Y%m%d}-a{attempt}-{symbol}"


@pytest.fixture(autouse=True)
def small_universe(monkeypatch):
    monkeypatch.setattr(paper, "universe", lambda: list(SYMBOLS))


@pytest.fixture
def env(tmp_path):
    store = Store(str(tmp_path / "bars.db"))
    for s in SYMBOLS:
        store.replace_daily(s, bars(s))
    store.set_daily_symbols(SYMBOLS)
    store.set_daily_through(T.isoformat())  # current: a run downloads nothing unless SPY is short
    broker = FakeBroker(NUMBER, sessions=SESSIONS, history=[(date(2026, 11, 11), 1e5), (T, 1e5)])
    acct = AccountStore(tmp_path / "paper", f"paper-{NUMBER}")
    acct.save_settings(strategy_path=str(BUY_SPY))

    def run(now=NOW, client=None, acct=acct):
        return paper.prepare_plan(broker, acct, store, client or FakeClient(), FakeActions(), now)

    def use(code):
        path = tmp_path / "strategy.py"
        path.write_text(code)
        acct.save_settings(strategy_path=str(path))

    return SimpleNamespace(store=store, broker=broker, acct=acct, run=run, use=use, tmp=tmp_path)


def refused(env, result, text):
    assert isinstance(result, Refusal) and text in result.reason
    assert env.acct.attempts(T) == []  # no journal


# --- the broker wrapper ---

def test_make_broker_is_always_paper(monkeypatch):
    seen = []
    monkeypatch.setattr(paper, "TradingClient", lambda *a, **k: seen.append(k) or SimpleNamespace())
    monkeypatch.setattr(paper, "CorporateActionsClient", lambda *a, **k: SimpleNamespace())
    assert isinstance(paper.make_broker(("KEY", "SECRET")), paper.Broker)
    assert seen == [{"paper": True}]
    app = Path(paper.__file__).parent
    hits = [p.name for p in app.rglob("*.py") for _ in range(p.read_text().count("TradingClient("))]
    assert sorted(hits) == ["paper.py", "ui.py"]  # ui.py: the calendar client, also paper=True
    assert "TradingClient(*keys, paper=True)" in (app / "ui.py").read_text()


def test_equity_history_dates_by_new_york_day():
    stamp = datetime(2026, 11, 12, 20, 0, tzinfo=ET).timestamp()  # 01:00 UTC on the 13th
    trading = SimpleNamespace(get_portfolio_history=lambda req: SimpleNamespace(timestamp=[int(stamp)],
                                                                                equity=[100000.0]))
    assert paper.Broker(trading, None).equity_history() == [(T, 100000.0)]


def test_submit_sends_a_whole_share_market_order():
    sent = []
    reply = SimpleNamespace(client_order_id=oid(1, "SPY"), id="u1", symbol="SPY", side=OrderSide.BUY, qty="3",
                            filled_qty="0", filled_avg_price=None, status=OrderStatus.ACCEPTED)
    trading = SimpleNamespace(submit_order=lambda req: sent.append(req) or reply)
    got = paper.Broker(trading, None).submit(Order("SPY", "buy", 3, oid(1, "SPY"), 100.0))
    assert got == BrokerOrder(oid(1, "SPY"), "u1", "SPY", "buy", 3.0, 0.0, None, "accepted")
    (req,) = sent
    assert (req.symbol, req.qty, req.side, req.time_in_force, req.client_order_id) == (
        "SPY", 3, OrderSide.BUY, TimeInForce.OPG, oid(1, "SPY"))


# --- preparing a plan ---

def test_plan_matches_backtest_targets(env):
    plan = env.run()
    assert plan.day == T and plan.next_day == T1 and plan.attempt == 1
    data = load_bars(env.store, NOW, SYMBOLS)
    result = backtest.run(backtest.buy_spy, data, BAR_DAYS[0], T, now=NOW)  # both sides on their defaults
    last = result.positions[result.positions["date"].isna()]
    assert plan.targets == {r.symbol: r.target_weight for r in last.itertuples() if r.target_weight}
    assert plan.dividends is True and result.dividends is True
    prices = backtest.prepare(data)
    assert plan.input_hash == backtest.input_hash(backtest.history_as_of(prices, prices.days[-1]))


def test_refuses_when_halted(env):
    env.acct.save_state(halted=True)
    refused(env, env.run(), "halted")


def test_refuses_for_another_account(env):
    refused(env, env.run(acct=AccountStore(env.tmp / "paper", "paper-PA0000002")), NUMBER)


def test_refuses_outside_window(env):
    refused(env, env.run(now=datetime(2026, 11, 13, 9, 28, tzinfo=ET)), "09:28")


def test_refuses_without_day_t_spy_bar(env):
    env.store.replace_daily("SPY", bars("SPY", through=date(2026, 11, 11)))
    client = FakeClient({s: [fake_day(d.isoformat(), CLOSES[s]) for d in BAR_DAYS[:-1]] for s in SYMBOLS})
    refused(env, env.run(client=client), "SPY")
    assert client.requests  # the current marker did not stop the download


def test_refuses_when_split_lookup_fails(env):
    env.broker.split_error = RuntimeError("corporate actions timed out")
    refused(env, env.run(), "corporate actions timed out")


def test_refuses_with_other_days_open_orders(env):
    env.broker.orders = [order(oid(1, "SPY", day=date(2026, 11, 11)), "accepted")]
    refused(env, env.run(), "open order")


@pytest.mark.parametrize("code, text", [(None, "strategy"), ("missing", "not found"),
                                        ("def target_weights(h, a):\n    1 / 0\n", "ZeroDivisionError")])
def test_refuses_without_a_working_strategy(env, code, text):
    if code is None:
        env.acct.save_settings(strategy_path=None)
    elif code == "missing":
        env.acct.save_settings(strategy_path=str(env.tmp / "gone.py"))
    else:
        env.use(code)
    refused(env, env.run(), text)


def test_split_on_next_day_blocks_symbol(env):
    env.use(FIFTHS)
    env.broker.split_dates = {"AAPL": T1, "SPY": date(2026, 11, 20)}  # SPY's is after T+1
    plan = env.run()
    assert [(o.symbol, o.side, o.qty) for o in plan.orders] == [("SPY", "buy", 196)]
    assert [n.symbol for n in plan.notes if n.kind == "blocked_split"] == ["AAPL"]


def test_attempt_counts_broker_orders(env):
    env.broker.orders = [order(oid(1, "SPY"), "filled")]
    plan = env.run()
    assert plan.attempt == 2 and [o.client_order_id for o in plan.orders] == [oid(2, "SPY")]
    assert any("another computer" in n.text for n in plan.notes)
    assert env.acct.attempts(T) == [2]


def test_unsent_remainder_is_offered(env):
    env.use(FIFTHS)
    first = env.run()
    assert [o.symbol for o in first.orders] == ["AAPL", "SPY"]
    j = env.acct.journal(T, 1)
    j.mark(oid(1, "SPY"), "sending")
    j.mark(oid(1, "SPY"), "accepted")
    env.broker.orders = [order(oid(1, "SPY"), "accepted")]  # this attempt's own open order is allowed
    again = env.run()
    assert again.attempt == 1 and again.orders == [o for o in first.orders if o.symbol == "AAPL"]
    assert env.acct.attempts(T) == [1]


def test_loss_breach_while_closed_locks(env):
    env.acct.save_state(loss_checked_through="2026-11-09")
    env.broker.history = [(date(2026, 11, 9), 1e5), (date(2026, 11, 10), 9.2e4),
                          (date(2026, 11, 11), 9.2e4), (T, 9.2e4)]
    plan = env.run()
    assert plan.orders == [] and [n.kind for n in plan.notes if n.symbol == "SPY"] == ["trim_cap", "blocked_loss"]
    state = env.acct.state()
    assert state["loss_lock"]["session"] == "2026-11-10" and state["loss_checked_through"] == T.isoformat()
    assert plan.loss_lock == state["loss_lock"]  # what sending rechecks against


def test_cleared_breach_does_not_relock_but_new_one_does(env):
    history = [(date(2026, 11, 9), 1e5), (date(2026, 11, 10), 9.2e4), (date(2026, 11, 11), 9.2e4), (T, 9.2e4)]
    for name, extra, lock in [("a", [], None), ("b", [(T, 8.5e4)], "2026-11-12")]:
        acct = AccountStore(env.tmp / name, f"paper-{NUMBER}")
        acct.save_settings(strategy_path=str(BUY_SPY))
        acct.save_state(loss_checked_through="2026-11-09", cleared=["2026-11-10"])
        env.broker.history = history[:-1] + (extra or history[-1:])
        plan = env.run(acct=acct)
        assert (acct.state()["loss_lock"] or {}).get("session") == lock
        assert bool(plan.orders) == (lock is None)


def test_plan_saved_before_return(env):
    env.acct.save_settings(dividends=True)
    env.broker.held = {"AAPL": 10.0}
    plan = env.run()
    j = env.acct.journal(T, 1)
    saved = j.plan()
    code = BUY_SPY.read_bytes()
    assert saved["input_hash"] == plan.input_hash and len(plan.input_hash) == 64
    assert saved["strategy_sha256"] == hashlib.sha256(code).hexdigest()
    assert (j.folder / "strategy.py").read_bytes() == code
    assert saved["dividends"] is True
    assert saved["loss"] == {"breaches": [], "checked_through": T.isoformat(), "problem": None}
    assert saved["last_bars"] == {s: dict(o=CLOSES[s], h=CLOSES[s], l=CLOSES[s], c=CLOSES[s], v=1000.0)
                                  for s in SYMBOLS}
    assert saved["data_date"] == T.isoformat() and saved["positions"] == {"AAPL": 10.0}
    assert j.closed() is None


def test_plan_round_trips_through_json(env):
    env.broker.held = {"XYZ": 5.0}  # outside the universe: sold with no close, so ref_price is NaN
    plan = env.run()
    xyz = next(o for o in plan.orders if o.symbol == "XYZ")
    assert math.isnan(xyz.ref_price)
    back = paper.plan_from_json(env.acct.journal(T, 1).plan())
    assert repr(back) == repr(plan)  # repr, because NaN != NaN


def day_t_from_live_equity(env, equity, lock):
    env.acct.save_state(loss_checked_through="2026-11-10")
    env.broker.history = [(date(2026, 11, 10), 1e5), (date(2026, 11, 11), 1e5)]  # no point for T yet
    env.broker.acct = paper.Account(NUMBER, "https://paper-api.alpaca.markets", equity, equity, 2 * equity)
    plan = env.run()
    state = env.acct.state()
    assert (state["loss_lock"] or {}).get("session") == lock and bool(plan.orders) == (lock is None)
    assert state["loss_checked_through"] == "2026-11-11"  # the next run re-checks T from real history
    assert any("live equity" in n.text for n in plan.notes)
    return plan


def test_day_t_loss_from_live_equity_locks(env):
    plan = day_t_from_live_equity(env, 95_000.0, "2026-11-12")
    assert [d for d, _ in plan.loss.breaches] == [T]


def test_day_t_small_move_from_live_equity_does_not_lock(env):
    plan = day_t_from_live_equity(env, 99_000.0, None)
    assert plan.loss.breaches == []


def test_edited_strategy_supersedes_an_unsent_attempt(env):
    assert env.run().targets == {"SPY": 1.0}
    env.use(FIFTHS)
    again = env.run()
    assert again.attempt == 2 and again.targets == {"SPY": 0.2, "AAPL": 0.2}
    assert env.acct.journal(T, 1).closed() == "superseded" and env.acct.journal(T, 2).closed() is None


def test_what_ran_is_what_was_saved(env, monkeypatch):
    real = paper.load_strategy

    def edit_then_load(path):  # the file changes after it was read for the copy
        (env.tmp / "strategy.py").write_text(FIFTHS)
        return real(path)

    env.use(BUY_SPY.read_text())
    monkeypatch.setattr(paper, "load_strategy", edit_then_load)  # the copy it loads is the bytes read first
    plan = env.run()
    j = env.acct.journal(T, 1)
    assert plan.targets == {"SPY": 1.0} and (j.folder / "strategy.py").read_bytes() == BUY_SPY.read_bytes()
    assert plan.strategy_sha256 == hashlib.sha256(BUY_SPY.read_bytes()).hexdigest()


def test_history_after_day_t_is_ignored(env):
    env.broker.history = [(date(2026, 11, 11), 1e5), (T, 1e5), (T1, 5e4)]
    plan = env.run()
    assert env.acct.state()["loss_checked_through"] == T.isoformat() and env.acct.state()["loss_lock"] is None
    assert plan.orders and plan.loss.checked_through == T


def test_load_error_names_the_chosen_file(env):
    env.use("def target_weights(:\n")
    refused(env, env.run(), f"cannot load {env.tmp / 'strategy.py'}:")


# --- sending ---

SEND = datetime(2026, 11, 12, 21, 30, tzinfo=ET)


def journaled(env, attempt=1):
    return [(e.get("id"), e.get("status")) for e in env.acct.journal(T, attempt).events() if "id" in e]


def test_send_submits_in_order_with_journal(env):
    env.use(FIFTHS)
    env.broker.held = {"XYZ": 5.0}
    plan = env.run()
    ids = [o.client_order_id for o in plan.orders]
    assert [o.side for o in plan.orders] == ["sell", "buy", "buy"]
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert result.discarded is None and result.failed == []
    assert [o.client_order_id for o in result.sent] == ids
    assert env.broker.submitted == [(i, "opg") for i in ids]
    assert journaled(env) == [x for i in ids for x in ((i, "sending"), (i, "accepted"))]
    assert env.acct.journal(T, 1).closed() == "sent"


def test_send_rechecks_positions(env):
    plan = env.run()
    env.broker.held = {"SPY": 1.0}
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert result.discarded and "Positions" in result.discarded
    assert env.broker.submitted == [] and env.acct.journal(T, 1).closed() == "discarded"


def test_send_after_deadline_discarded(env):
    plan = env.run()
    result = paper.send_plan(env.broker, env.acct, plan, datetime(2026, 11, 13, 9, 28, tzinfo=ET))
    assert result.discarded and "09:28" in result.discarded and env.broker.submitted == []


@pytest.mark.parametrize("change, text", [
    (lambda env: env.acct.save_state(halted=True), "halted"),
    (lambda env: env.acct.save_state(loss_lock={"session": "2026-11-12", "loss": -0.05, "problem": None}), "loss"),
    (lambda env: env.acct.save_settings(limits=paper.Limits(max_weight=0.5)), "safety"),
    (lambda env: setattr(env.broker, "orders", [order(oid(2, "SPY"), "accepted")]), "open order"),
    (lambda env: setattr(env.broker, "acct", paper.Account("PA0000002", "x", 1e5, 1e5, 2e5)), "PA0000002"),
    (lambda env: setattr(env.broker, "acct", paper.Account(NUMBER, "https://api.alpaca.markets", 1e5, 1e5, 2e5)),
     "api.alpaca.markets"),
    (lambda env: setattr(env.broker, "held", {"SPY": 1e-6}), "Positions"),
])
def test_send_recheck_discards(env, change, text):
    plan = env.run()
    change(env)
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert text in result.discarded and env.broker.submitted == []
    assert env.acct.journal(T, 1).closed() == "discarded"


def test_send_allows_this_attempts_open_orders_and_tiny_rounding(env):
    env.use(FIFTHS)
    env.broker.held = {"AAPL": 2.0}
    plan = env.run()
    env.broker.held = {"AAPL": 2.0 + 1e-12}
    env.broker.orders = [order(oid(1, "AAPL"), "accepted")]
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert result.discarded is None and [o.client_order_id for o in result.sent] == [oid(1, "AAPL"), oid(1, "SPY")]


def test_send_holds_the_account_lock(env):
    plan = env.run()
    with env.acct.lock(), pytest.raises(Busy):
        paper.send_plan(env.broker, env.acct, plan, SEND)
    assert env.broker.submitted == []


def test_send_stops_when_halted_midway(env):
    env.use(FIFTHS)
    plan = env.run()
    env.broker.on_submit = lambda n: env.acct.save_state(halted=True)
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert env.broker.submitted == [(oid(1, "AAPL"), "opg")] and result.discarded is None
    assert journaled(env) == [(oid(1, "AAPL"), "sending"), (oid(1, "AAPL"), "accepted")]
    assert env.acct.journal(T, 1).closed() is None


@pytest.mark.parametrize("error", [None, rejection(DUPLICATE, 422)])  # None: the fake's plain refusal
def test_duplicate_id_counts_as_sent(env, error):
    plan = env.run()
    env.broker.orders = [order(oid(1, "SPY"), "new")]  # it reached Alpaca before
    if error:  # Alpaca's real answer is a 422, so the duplicate check must come before the 4xx one
        env.broker.on_submit = lambda n: (_ for _ in ()).throw(error)
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert result.failed == [] and result.sent == env.broker.orders and env.broker.submitted == []
    assert journaled(env) == [(oid(1, "SPY"), "sending"), (oid(1, "SPY"), "new")]
    assert env.acct.journal(T, 1).closed() == "sent"


def test_duplicate_id_alpaca_cannot_find_is_not_found(env):
    plan = env.run()

    def refuse(n):
        raise Exception(DUPLICATE)

    env.broker.on_submit = refuse
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert result.failed == [] and result.sent == []
    assert journaled(env) == [(oid(1, "SPY"), "sending"), (oid(1, "SPY"), "not_found")]
    assert env.acct.journal(T, 1).closed() is None


def test_duplicate_id_whose_lookup_fails_is_not_found(env, monkeypatch):
    plan = env.run()
    env.broker.on_submit = lambda n: (_ for _ in ()).throw(rejection(DUPLICATE, 422))
    monkeypatch.setattr(env.broker, "order", lambda cid: (_ for _ in ()).throw(ConnectionError("down")))
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert result.failed == [] and result.sent == []
    assert journaled(env) == [(oid(1, "SPY"), "sending"), (oid(1, "SPY"), "not_found")]


def test_rejection_marks_failed(env):
    env.use(FIFTHS)
    plan = env.run()
    body = '{"code":42210000,"message":"asset AAPL is not tradable"}'

    def refuse(n):
        if n == 1:
            raise rejection(body, 422)

    env.broker.on_submit = refuse
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert result.failed == [(oid(1, "AAPL"), body)] and result.sent == [] and env.broker.calls == 1
    j = env.acct.journal(T, 1)
    assert j.events()[-1]["status"] == "failed" and j.events()[-1]["detail"] == {"error": body}
    assert j.closed() is None
    again = env.run()  # the rest is offered once the error has been seen
    assert again.attempt == 1 and [o.client_order_id for o in again.orders] == [oid(1, "SPY")]


def test_timeout_with_order_at_alpaca_is_recorded(env):
    env.use(FIFTHS)
    plan = env.run()

    def lost_answer(n):
        if n == 1:  # the order reached Alpaca, but its answer never came back
            env.broker.orders.append(order(oid(1, "AAPL"), "accepted"))
            raise ConnectionError("read timed out")

    env.broker.on_submit = lost_answer
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert result.failed == [] and [o.client_order_id for o in result.sent] == [oid(1, "AAPL"), oid(1, "SPY")]
    assert env.acct.journal(T, 1).order_states() == {oid(1, "AAPL"): "accepted", oid(1, "SPY"): "accepted"}
    assert env.acct.journal(T, 1).closed() == "sent"


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_timeout_without_order_stays_sending(env, monkeypatch, lookup_fails):
    env.use(FIFTHS)
    plan = env.run()

    def no_answer(n):
        raise ConnectionError("connection reset")

    env.broker.on_submit = no_answer
    if lookup_fails:
        monkeypatch.setattr(env.broker, "order", lambda cid: (_ for _ in ()).throw(ConnectionError("down")))
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert result.sent == [] and env.broker.calls == 1
    assert [cid for cid, _ in result.failed] == [oid(1, "AAPL")] and "no answer" in result.failed[0][1]
    j = env.acct.journal(T, 1)
    assert journaled(env) == [(oid(1, "AAPL"), "sending")] and j.closed() is None
    assert [(x.folder, cid) for x, cid in env.acct.unresolved()] == [(j.folder, oid(1, "AAPL"))]


def test_recover_resolves_sending(env):
    env.use(FIFTHS)
    first = env.run()
    j = env.acct.journal(T, 1)
    j.mark(oid(1, "AAPL"), "sending")
    j.mark(oid(1, "SPY"), "sending")
    env.broker.orders = [order(oid(1, "AAPL"), "accepted")]
    lines = paper.recover(env.broker, env.acct)
    assert len(lines) == 2 and env.acct.unresolved() == []
    assert j.order_states() == {oid(1, "AAPL"): "accepted", oid(1, "SPY"): "not_found"}
    again = env.run()  # an order Alpaca never got is offered again, same ID and quantity
    assert again.attempt == 1 and again.orders == [o for o in first.orders if o.symbol == "SPY"]


def test_recover_closes_an_attempt_that_had_every_result(env):
    plan = env.run()
    j = env.acct.journal(T, 1)
    j.mark(oid(1, "SPY"), "sending")
    env.broker.orders = [order(oid(1, "SPY"), "accepted")]
    paper.recover(env.broker, env.acct)
    assert j.closed() == "sent" and plan.orders[0].client_order_id == oid(1, "SPY")


def test_send_twice_never_doubles(env):
    env.use(FIFTHS)
    plan = env.run()
    paper.send_plan(env.broker, env.acct, plan, SEND)
    again = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert again.discarded and again.sent == []
    ids = [o.client_order_id for o in env.broker.orders]
    assert sorted(ids) == [oid(1, "AAPL"), oid(1, "SPY")]
    assert env.acct.journal(T, 1).closed() == "sent"


def test_server_error_with_order_at_alpaca_is_recorded(env):
    plan = env.run()

    def created_then_503(n):  # a 5xx can come back after Alpaca created the order
        env.broker.orders.append(order(oid(1, "SPY"), "accepted"))
        raise rejection('{"message":"service unavailable"}', 503)

    env.broker.on_submit = created_then_503
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert result.failed == [] and [o.client_order_id for o in result.sent] == [oid(1, "SPY")]
    assert journaled(env) == [(oid(1, "SPY"), "sending"), (oid(1, "SPY"), "accepted")]


def halted_after_first(env):
    env.use(FIFTHS)
    plan = env.run()
    env.broker.on_submit = lambda n: env.acct.save_state(halted=True)
    paper.send_plan(env.broker, env.acct, plan, SEND)
    env.broker.on_submit = None
    return plan


def test_failed_recheck_keeps_a_partly_sent_attempt_open(env):
    plan = halted_after_first(env)
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert "halted" in result.discarded and env.acct.journal(T, 1).closed() is None
    assert env.broker.submitted == [(oid(1, "AAPL"), "opg")]


def test_resend_after_halt_skips_sent_orders(env):
    plan = halted_after_first(env)
    env.acct.save_state(halted=False)
    result = paper.send_plan(env.broker, env.acct, plan, SEND)  # the same full plan
    assert result.discarded is None and [o.client_order_id for o in result.sent] == [oid(1, "SPY")]
    assert env.broker.submitted == [(oid(1, "AAPL"), "opg"), (oid(1, "SPY"), "opg")] and env.broker.calls == 2
    assert env.acct.journal(T, 1).closed() == "sent"


def test_unknown_sending_order_blocks_a_second_send(env):
    env.use(FIFTHS)
    plan = env.run()

    def no_answer(n):
        raise ConnectionError("connection reset")

    env.broker.on_submit = no_answer
    paper.send_plan(env.broker, env.acct, plan, SEND)
    env.broker.on_submit = None
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert env.broker.calls == 1 and env.broker.submitted == [] and result.sent == []
    assert [cid for cid, _ in result.failed] == [oid(1, "AAPL")] and "no answer" in result.failed[0][1]
    assert journaled(env) == [(oid(1, "AAPL"), "sending")] and env.acct.journal(T, 1).closed() is None


def test_sending_order_found_on_resend_is_recorded_not_resubmitted(env):
    env.use(FIFTHS)
    plan = env.run()
    env.acct.journal(T, 1).mark(oid(1, "AAPL"), "sending")  # a crash, and no recover() since
    env.broker.orders = [order(oid(1, "AAPL"), "accepted")]
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert env.broker.submitted == [(oid(1, "SPY"), "opg")] and env.broker.calls == 1 and result.failed == []
    assert env.acct.journal(T, 1).order_states() == {oid(1, "AAPL"): "accepted", oid(1, "SPY"): "accepted"}


def test_order_alpaca_reports_rejected_is_a_failure(env):
    env.use(FIFTHS)
    plan = env.run()
    env.broker.orders = [order(oid(1, "AAPL"), "rejected")]  # duplicate: the lookup finds it rejected
    result = paper.send_plan(env.broker, env.acct, plan, SEND)
    assert result.sent == [] and result.failed == [(oid(1, "AAPL"), "rejected")]
    assert journaled(env) == [(oid(1, "AAPL"), "sending"), (oid(1, "AAPL"), "rejected")]
    assert env.broker.calls == 1  # a rejection stops the rest, like a failed request


# --- kill switch, release and sell everything ---

WINDOW_CLOSED = "the at-the-open window has closed; wait for the market to open"


def no_wait(seconds):
    pass


def partly_sent(env):
    """A daily attempt left open: the XYZ sale is at Alpaca, the AAPL buy was refused, the SPY buy never sent."""
    env.use(FIFTHS)
    env.broker.held = {"XYZ": 5.0}
    plan = env.run()
    env.broker.on_submit = lambda n: n == 2 and (_ for _ in ()).throw(rejection('{"message":"no"}', 422))
    paper.send_plan(env.broker, env.acct, plan, SEND)
    env.broker.on_submit = None
    assert env.acct.journal(T, 1).order_states() == {oid(1, "XYZ"): "accepted", oid(1, "AAPL"): "failed"}
    assert env.acct.journal(T, 1).closed() is None


def test_kill_halts_before_cancelling(env):
    partly_sent(env)
    seen = []
    env.broker.on_cancel = lambda: seen.append(env.acct.state()["halted"])
    result = paper.kill(env.broker, env.acct, sleep=no_wait)
    assert seen == [True]
    assert sorted(result) == [(oid(1, "AAPL"), "failed"), (oid(1, "XYZ"), "canceled")]
    j = env.acct.journal(T, 1)
    assert j.order_states()[oid(1, "XYZ")] == "canceled" and j.closed() == "killed"


def test_kill_cancels_an_order_that_got_out_after_its_cancel(env):
    partly_sent(env)
    env.acct.journal(T, 1).mark(oid(1, "SPY"), "sending")  # the sender passed its halt check just before the kill
    cancels = []
    env.broker.on_cancel = lambda: cancels.append(1)

    def lands(seconds):  # the order reaches Alpaca after the first cancel, while kill is confirming
        if not env.broker.order(oid(1, "SPY")):
            env.broker.orders.append(order(oid(1, "SPY"), "accepted"))

    result = paper.kill(env.broker, env.acct, sleep=lands)
    assert dict(result)[oid(1, "SPY")] == "canceled" and env.broker.order(oid(1, "SPY")).status == "canceled"
    assert len(cancels) >= 2 and env.acct.journal(T, 1).closed() == "killed"
    assert paper.unresolved_cancels(env.broker, env.acct) == []


def test_alpacas_open_list_beats_a_journal_that_calls_an_order_finished(env):
    partly_sent(env)
    paper.kill(env.broker, env.acct, sleep=no_wait)
    env.acct.journal(T, 2).mark(oid(2, "SPY"), "not_found")  # an attempt still open, wrong about its order
    env.broker.orders.append(order(oid(2, "SPY"), "accepted"))  # Alpaca has it open
    assert oid(2, "SPY") in paper.unresolved_cancels(env.broker, env.acct)
    assert oid(2, "SPY") in paper.release(env.broker, env.acct) and env.acct.state()["halted"]
    assert env.acct.journal(T, 2).closed() is None


def test_release_waits_for_cancels(env):
    paper.send_plan(env.broker, env.acct, env.run(), SEND)  # attempt 1 is closed "sent", its order still open
    env.broker.cancel_to = "pending_cancel"
    naps = []
    assert paper.kill(env.broker, env.acct, sleep=naps.append) == [(oid(1, "SPY"), "pending_cancel")]
    assert len(naps) == 10  # polled until the timeout, then gave up
    assert paper.unresolved_cancels(env.broker, env.acct) == [oid(1, "SPY")]
    why = paper.release(env.broker, env.acct)
    assert oid(1, "SPY") in why and env.acct.state()["halted"]
    env.broker.orders = [replace(o, status="canceled") for o in env.broker.orders]
    assert paper.unresolved_cancels(env.broker, env.acct) == []
    assert paper.release(env.broker, env.acct) is None and env.acct.state()["halted"] is False
    last = json.loads((env.acct.dir / "events.jsonl").read_text().splitlines()[-1])
    assert last["kind"] == "state" and last["new"] == {"halted": False}  # logged


def test_kill_journals_the_cancel_of_a_sent_attempt(env):
    paper.send_plan(env.broker, env.acct, env.run(), SEND)  # attempt 1 is closed "sent", its order still open
    paper.kill(env.broker, env.acct, sleep=no_wait)
    j = env.acct.journal(T, 1)
    assert j.order_states()[oid(1, "SPY")] == "canceled"  # the live E2E's journal check
    assert j.closed() == "killed"  # the kill cancelled one of its orders
    assert paper.release(env.broker, env.acct) is None and env.acct.state()["halted"] is False


def test_a_refresh_journaling_the_kills_cancel_first_still_closes_killed(env):
    paper.send_plan(env.broker, env.acct, env.run(), SEND)
    env.broker.cancel_to = "pending_cancel"
    paper.kill(env.broker, env.acct, sleep=no_wait)  # gives up with the cancel unconfirmed
    env.broker.orders = [replace(o, status="canceled") for o in env.broker.orders]
    assert paper.unresolved_cancels(env.broker, env.acct) == []  # the status refresh journals "canceled"
    j = env.acct.journal(T, 1)
    assert j.order_states()[oid(1, "SPY")] == "canceled" and j.closed() == "sent"
    assert paper.release(env.broker, env.acct) is None and j.closed() == "killed"


def test_kill_leaves_an_attempt_cancelled_before_it_closed_sent(env, monkeypatch):
    paper.send_plan(env.broker, env.acct, env.run(), SEND)
    env.broker.orders = [replace(o, status="canceled") for o in env.broker.orders]  # cancelled by hand earlier
    with monkeypatch.context() as m:
        m.setattr(paper_state, "_now", lambda: "2020-01-01T00:00:00+00:00")
        assert paper.unresolved_cancels(env.broker, env.acct) == []
    paper.kill(env.broker, env.acct, sleep=no_wait)
    j = env.acct.journal(T, 1)
    assert j.order_states()[oid(1, "SPY")] == "canceled" and j.closed() == "sent"


def test_kill_leaves_a_finished_sent_attempt_closed_sent(env):
    paper.send_plan(env.broker, env.acct, env.run(), SEND)
    env.broker.orders = [replace(o, status="filled") for o in env.broker.orders]  # filled before the kill
    paper.kill(env.broker, env.acct, sleep=no_wait)
    j = env.acct.journal(T, 1)
    assert j.order_states()[oid(1, "SPY")] == "filled" and j.closed() == "sent"


def test_done_for_day_order_does_not_block_release(env):
    paper.send_plan(env.broker, env.acct, env.run(), SEND)
    env.acct.save_state(halted=True)
    env.broker.orders = [replace(o, status="done_for_day") for o in env.broker.orders]
    assert paper.release(env.broker, env.acct) is None and env.acct.state()["halted"] is False


def test_new_attempt_after_release(env):
    partly_sent(env)
    paper.kill(env.broker, env.acct, sleep=no_wait)
    assert isinstance(env.run(), Refusal)  # halted
    assert paper.release(env.broker, env.acct) is None
    env.broker.held = {"SPY": 50.0}
    plan = env.run()
    assert plan.attempt == 2 and env.acct.journal(T, 1).closed() == "killed"
    assert [(o.client_order_id, o.qty) for o in plan.orders] == [(oid(2, "AAPL"), 392), (oid(2, "SPY"), 146)]


@pytest.mark.parametrize("sell_all", [False, True])
def test_kill_during_a_send_lists_the_order_that_got_out(env, sell_all):
    env.use(FIFTHS)
    env.broker.held = {"AAPL": 3.0, "SPY": 2.0}
    plan = paper.plan_sell_all(env.broker, env.acct, SEND) if sell_all else env.run()
    send = paper.send_sell_all if sell_all else paper.send_plan
    killed = []
    # the kill lands between the sender's halt check and its submit; the send holds the account lock throughout
    env.broker.on_submit = lambda n: n == 1 and killed.append(paper.kill(env.broker, env.acct, sleep=no_wait))
    send(env.broker, env.acct, plan, SEND)
    assert env.broker.submitted == [(oid(1, "AAPL"), "opg")]  # nothing more after the kill
    assert killed == [[(oid(1, "AAPL"), "sending")]]
    j = env.acct.journal(T, 1)
    assert j.order_states() == {oid(1, "AAPL"): "accepted"} and j.closed() is None
    assert paper.unresolved_cancels(env.broker, env.acct) == [oid(1, "AAPL")]
    assert oid(1, "AAPL") in paper.release(env.broker, env.acct)
    paper.kill(env.broker, env.acct, sleep=no_wait)  # a second kill cancels it
    assert paper.release(env.broker, env.acct) is None and j.closed() == "killed"


def test_sell_all_waits_for_open_buys(env):
    paper.send_plan(env.broker, env.acct, env.run(), SEND)  # an open at-the-open buy
    env.broker.held = {"AAPL": 3.0}
    result = paper.plan_sell_all(env.broker, env.acct, SEND)
    assert isinstance(result, Refusal) and "open order" in result.reason and env.acct.attempts(T) == [1]
    env.broker.orders = [replace(o, status="filled") for o in env.broker.orders]
    env.broker.held = {"AAPL": 3.0, "SPY": 980.4}
    plan = paper.plan_sell_all(env.broker, env.acct, SEND)
    assert env.acct.state()["halted"] and plan.sell_all and plan.attempt == 2
    assert [(o.client_order_id, o.side, o.qty) for o in plan.orders] == [
        (oid(2, "AAPL"), "sell", 3), (oid(2, "SPY"), "sell", 980)]
    assert [(n.symbol, n.kind) for n in plan.notes] == [("SPY", "fractional")]
    assert plan.loss_lock == env.acct.state()["loss_lock"]
    assert repr(paper.plan_from_json(env.acct.journal(T, 2).plan())) == repr(plan)  # journaled; repr for NaN


class Crash(BaseException):
    pass


def test_sell_all_survives_restart(env):
    env.broker.held = {"AAPL": 3.0, "SPY": 2.0}
    plan = paper.plan_sell_all(env.broker, env.acct, SEND)

    def crash(n):
        env.broker.orders.append(order(oid(1, "AAPL"), "accepted"))  # it reached Alpaca, then the app died
        raise Crash

    env.broker.on_submit = crash
    with pytest.raises(Crash):
        paper.send_sell_all(env.broker, env.acct, plan, SEND)
    env.broker.on_submit = None
    assert journaled(env) == [(oid(1, "AAPL"), "sending")]
    assert paper.recover(env.broker, env.acct) == [f"{oid(1, 'AAPL')}: accepted"]
    again = paper.plan_sell_all(env.broker, env.acct, SEND)
    assert again.attempt == 1 and [o.client_order_id for o in again.orders] == [oid(1, "SPY")]
    result = paper.send_sell_all(env.broker, env.acct, again, SEND)
    assert result.discarded is None and [o.client_order_id for o in result.sent] == [oid(1, "SPY")]
    assert env.acct.journal(T, 1).closed() == "sent"


@pytest.mark.parametrize("is_open, now, tif", [
    (True, datetime(2026, 11, 13, 10, 0, tzinfo=ET), "day"),
    (False, SEND, "opg"),
    (False, datetime(2026, 11, 13, 9, 28, tzinfo=ET), None),
])
def test_sell_all_order_type(env, is_open, now, tif):
    env.broker.held = {"SPY": 2.0}
    env.broker.is_open = is_open
    plan = paper.plan_sell_all(env.broker, env.acct, now)
    assert (plan.day, plan.next_day) == (T, T1)
    result = paper.send_sell_all(env.broker, env.acct, plan, now)
    if tif:
        assert result.discarded is None and env.broker.submitted == [(oid(1, "SPY"), tif)]
    else:
        assert result.discarded == WINDOW_CLOSED and env.broker.submitted == []


def test_sell_all_send_refuses_a_buy(env):
    env.broker.held = {"SPY": 2.0}
    plan = paper.plan_sell_all(env.broker, env.acct, SEND)
    bad = replace(plan, orders=[*plan.orders, Order("AAPL", "buy", 1, oid(1, "AAPL"), 50.0)])
    with pytest.raises(ValueError):
        paper.send_sell_all(env.broker, env.acct, bad, SEND)
    assert env.broker.calls == 0 and journaled(env) == []


# --- fills, drift reasons, replay, export ---

def sent_plan(env, code=FIFTHS):
    env.use(code)
    plan = env.run()
    paper.send_plan(env.broker, env.acct, plan, SEND)
    return plan


def fill_order(env, symbol, qty, price, day=T):
    cid = oid(1, symbol, day)
    env.broker.orders = [replace(o, filled_qty=qty, filled_avg_price=price,
                                 status="filled" if qty else o.status) if o.client_order_id == cid else o
                         for o in env.broker.orders]


def add_next_bar(env, opens):
    for s, o in opens.items():
        c = CLOSES[s]
        env.store.replace_daily(s, [*bars(s), DailyBar(s, T1.isoformat(), o, o, o, c, 1000.0, 1.0, 1.0)])


def row(fills, symbol):
    return fills[fills["symbol"] == symbol].iloc[0]


def test_signed_bps_cost_positive_both_sides(env):
    env.broker.held = {"AAPL": 400.0}  # target 0.2 -> 392: AAPL is sold
    sent_plan(env)
    add_next_bar(env, {"SPY": 100.0, "AAPL": 100.0})
    fill_order(env, "SPY", 196.0, 101.0)
    fill_order(env, "AAPL", 8.0, 99.0)
    fills = paper.track_fills(env.broker, env.acct, env.store)
    assert row(fills, "SPY").bps_vs_open == pytest.approx(100.0)
    assert row(fills, "AAPL").bps_vs_open == pytest.approx(100.0)
    assert row(fills, "SPY").bps_vs_close == pytest.approx(100.0)  # SPY's close is 100 too
    assert list(fills.columns) == ["day", "symbol", "side", "ordered", "filled", "avg_price", "open_next", "close_t",
                                   "bps_vs_open", "bps_vs_close", "backtest_shares", "planned_shares",
                                   "sent_shares", "filled_shares", "reasons"]


def test_cost_summary_weights_by_value():
    import pandas as pd
    f = pd.DataFrame({"filled": [10.0, 30.0], "avg_price": [10.0, 10.0], "bps_vs_open": [100.0, 20.0],
                      "bps_vs_close": [10.0, float("nan")]})
    got = paper.cost_summary(f)
    assert got["bps_vs_open"] == pytest.approx(40.0)
    assert got["bps_vs_close"] == pytest.approx(10.0)


def test_reasons_name_rounding_and_cap(env):
    env.use("def target_weights(history, as_of):\n    return {'SPY': 0.3, 'AAPL': 0.2}\n")
    plan = env.run()
    paper.send_plan(env.broker, env.acct, plan, SEND)
    fills = paper.track_fills(env.broker, env.acct, env.store)
    spy, aapl = row(fills, "SPY"), row(fills, "AAPL")
    assert "size_cap" in spy.reasons.split(";") and "cash_buffer" in spy.reasons.split(";")
    assert "size_cap" not in aapl.reasons and "cash_buffer" in aapl.reasons
    assert "rounding" not in aapl.reasons  # 0.2 * 1e5 / 50 = 400 exactly


def test_rounding_reason_when_a_share_is_fractional(env):
    sent_plan(env, "def target_weights(history, as_of):\n    return {'SPY': 0.2005}\n")
    assert "rounding" in row(paper.track_fills(env.broker, env.acct, env.store), "SPY").reasons.split(";")


def test_unfilled_opg_recorded(env):
    sent_plan(env)
    add_next_bar(env, {"SPY": 100.0, "AAPL": 50.0})
    fill_order(env, "AAPL", 392.0, 50.0)
    fills = paper.track_fills(env.broker, env.acct, env.store)
    r = row(fills, "SPY")
    assert r.filled == 0 and "unfilled" in r.reasons.split(";")
    assert math.isnan(r.avg_price) and math.isnan(r.bps_vs_open)
    assert r.sent_shares == 196 and r.filled_shares == 0


def test_loss_lock_and_split_block_rows(env):
    env.acct.save_state(loss_lock={"session": T.isoformat(), "loss": -0.05, "problem": None})
    env.broker.held = {"XYZ": 5.0}  # the sale goes out; the buys are blocked
    sent_plan(env)
    fills = paper.track_fills(env.broker, env.acct, env.store)
    assert sorted(fills.symbol) == ["AAPL", "SPY", "XYZ"]
    assert row(fills, "SPY").reasons.split(";").count("loss_lock") == 1 and row(fills, "SPY").sent_shares == 0


def test_rows_stay_fixed_once_next_bar_exists(env):
    sent_plan(env)
    first = paper.track_fills(env.broker, env.acct, env.store)
    assert math.isnan(row(first, "SPY").open_next)
    add_next_bar(env, {"SPY": 100.0, "AAPL": 50.0})
    fill_order(env, "SPY", 196.0, 100.0)
    final = paper.track_fills(env.broker, env.acct, env.store)
    assert row(final, "SPY").filled == 196 and row(final, "SPY").open_next == 100.0
    fill_order(env, "SPY", 1.0, 999.0)  # later changes at the broker are not picked up
    again = paper.track_fills(env.broker, env.acct, env.store)
    assert row(again, "SPY").filled == 196
    text = (env.acct.journal(T, 1).folder / "fills.json").read_text()
    assert "KEY" not in text and "Traceback" not in text


def test_backtest_shares_sized_from_next_open(env):
    sent_plan(env)
    add_next_bar(env, {"SPY": 200.0, "AAPL": 50.0})  # cash 1e5 -> SPY 0.2*1e5/200 = 100
    fills = paper.track_fills(env.broker, env.acct, env.store)
    assert row(fills, "SPY").backtest_shares == pytest.approx(100.0)
    assert row(fills, "SPY").planned_shares == 196


def test_replay_matches_then_flags_changed_data(env):
    env.use(FIFTHS)
    env.run()
    j = env.acct.journal(T, 1)
    assert paper.replay(env.acct, j, env.store) == {"hash_matches": True, "same_targets": True,
                                                    "targets": {"SPY": 0.2, "AAPL": 0.2}}
    changed = [replace(b, c=b.c + 1) if b.date == T.isoformat() else b for b in bars("SPY")]
    env.store.replace_daily("SPY", changed)
    assert paper.replay(env.acct, j, env.store) == {"hash_matches": False, "same_targets": None, "targets": None}


def test_impure_strategy_shows_as_mismatch(env):
    import builtins
    code = ("import builtins\n"
            "def target_weights(history, as_of):\n"
            "    builtins._replay_n = getattr(builtins, '_replay_n', 0) + 1\n"
            "    return {'SPY': 0.1 * builtins._replay_n}\n")
    env.use(code)
    try:
        env.run()
        got = paper.replay(env.acct, env.acct.journal(T, 1), env.store)
    finally:
        vars(builtins).pop("_replay_n", None)
    assert got["hash_matches"] is True and got["same_targets"] is False


def test_replay_of_a_sell_all_journal_does_not_raise(env):
    env.broker.held = {"XYZ": 5.0}
    plan = paper.plan_sell_all(env.broker, env.acct, NOW)
    assert not isinstance(plan, Refusal)
    got = paper.replay(env.acct, env.acct.journal(plan.day, plan.attempt), env.store)
    assert got == {"hash_matches": None, "same_targets": None, "targets": None, "sell_all": True}


def test_to_cash_backtest_is_zero_shares(env):
    env.broker.held = {"AAPL": 400.0}
    sent_plan(env, "def target_weights(history, as_of):\n    return {}\n")
    add_next_bar(env, {"SPY": 100.0, "AAPL": 50.0})
    assert row(paper.track_fills(env.broker, env.acct, env.store), "AAPL").backtest_shares == 0


def test_export_fills_round_trips(env, tmp_path):
    import pandas as pd
    sent_plan(env)
    fills = paper.track_fills(env.broker, env.acct, env.store)
    out = tmp_path / "fills.csv"
    paper.export_fills(fills, str(out))
    back = pd.read_csv(out)
    assert list(back.columns) == list(fills.columns)
    assert back["symbol"].tolist() == fills["symbol"].tolist()
    assert not list(tmp_path.glob("*.tmp"))


def test_missed_sessions_lists_days_without_a_sent_attempt(env):
    sent_plan(env)  # T = Nov 12 sent
    late = datetime(2026, 11, 19, 12, 0, tzinfo=ET)
    got = paper.missed_sessions(env.acct, SESSIONS, now=late)
    assert got == [date(2026, 11, d) for d in (13, 16, 17, 18)]  # the 19th's window is still open
    assert paper.missed_sessions(env.acct, SESSIONS, now=datetime(2026, 11, 13, 8, 0, tzinfo=ET)) == []


def test_negative_cash_days_from_plans_and_live_account(env, monkeypatch):
    other = date(2026, 12, 1)
    monkeypatch.setattr(paper, "_today", lambda: other)
    sent_plan(env)
    assert paper.negative_cash_days(env.acct, env.broker) == []
    j = env.acct.journal(T, 1)
    (j.folder / "plan.json").write_text(json.dumps({**j.plan(), "account": {**j.plan()["account"], "cash": -5.0}}))
    assert paper.negative_cash_days(env.acct, env.broker) == [(T, -5.0)]
    env.broker.acct = replace(env.broker.acct, cash=-9.0)
    assert paper.negative_cash_days(env.acct, env.broker) == [(T, -5.0), (other, -9.0)]


def test_negative_snapshot_survives_positive_live_cash_on_the_same_day(env, monkeypatch):
    monkeypatch.setattr(paper, "_today", lambda: T)
    sent_plan(env)
    j = env.acct.journal(T, 1)
    (j.folder / "plan.json").write_text(json.dumps({**j.plan(), "account": {**j.plan()["account"], "cash": -5.0}}))
    assert paper.negative_cash_days(env.acct, env.broker) == [(T, -5.0)]


# --- paper lines for the compare screen ---

def filled_journal(env, day, nxt, sha, name="mine", filled=1.0, attempt=1, sell_all=False):
    base = paper.plan_to_json(env.plan)
    plan = {**base, "day": day.isoformat(), "next_day": nxt.isoformat(), "strategy_sha256": sha,
            "strategy_name": name, "attempt": attempt,
            "sell_all": sell_all}
    j = env.acct.journal(day, attempt)
    j.write_plan(plan)
    (j.folder / "fills.json").write_text(json.dumps({"final": True, "rows": [{"filled_shares": filled}]}))


@pytest.fixture(autouse=False)
def env_plan(env):
    env.plan = env.run()  # its own journal (T, a1) has no fills, so it makes no segment
    return env


def curve(*days):
    return [(date(2026, 11, d), 1000.0 + i) for i, d in enumerate(days)]


def test_plan_strategy_name_is_the_file_stem_and_defaults_for_old_plans(env):
    plan = env.run()
    assert plan.strategy_name == "buy_spy"
    old = paper.plan_to_json(plan)
    del old["strategy_name"]
    assert paper.plan_from_json(old).strategy_name == "strategy"


def test_segment_starts_at_prior_close(env_plan):
    env = env_plan
    filled_journal(env, date(2026, 11, 9), date(2026, 11, 10), "a" * 64)  # Mon's plan, fills Tue
    (name, df), = paper.paper_lines(env.acct, curve(5, 6, 9, 10, 11))
    assert name == "Paper: mine (aaaaaaa)"
    assert [d.day for d in df["date"]] == [9, 10, 11] and list(df.columns) == ["date", "value"]


def test_code_edit_starts_new_segment(env_plan):
    env = env_plan
    filled_journal(env, date(2026, 11, 9), date(2026, 11, 10), "a" * 64)
    filled_journal(env, date(2026, 11, 11), date(2026, 11, 12), "b" * 64, name="v2")
    filled_journal(env, date(2026, 11, 12), date(2026, 11, 13), "b" * 64, name="v2", attempt=2)  # same code: same line
    (a, da), (b, db) = paper.paper_lines(env.acct, curve(6, 9, 10, 11, 12, 13) + [(date(2026, 11, 14), 0.0)])
    assert (a, b) == ("Paper: mine (aaaaaaa)", "Paper: v2 (bbbbbbb)")
    assert [d.day for d in da["date"]] == [9, 10, 11] and [d.day for d in db["date"]] == [11, 12, 13]  # zero dropped


def test_unfilled_and_sell_all_attempts_make_no_segment(env_plan):
    env = env_plan
    filled_journal(env, date(2026, 11, 9), date(2026, 11, 10), "a" * 64, filled=0.0)
    filled_journal(env, date(2026, 11, 10), date(2026, 11, 11), "b" * 64, sell_all=True)
    assert paper.paper_lines(env.acct, curve(9, 10, 11)) == []


def test_repeated_strategy_gets_a_distinct_name(env_plan):
    env = env_plan
    for day, nxt, sha in ((9, 10, "a"), (10, 11, "b"), (11, 12, "a")):
        filled_journal(env, date(2026, 11, day), date(2026, 11, nxt), sha * 64)
    got = paper.paper_lines(env.acct, curve(9, 10, 11, 12, 13))
    assert [n for n, _ in got] == ["Paper: mine (aaaaaaa)", "Paper: mine (bbbbbbb)", "Paper: mine (aaaaaaa) #2"]
