import hashlib
import math
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce

from app import backtest, paper
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
    assert hits == ["paper.py"]


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
    result = backtest.run(backtest.buy_spy, data, BAR_DAYS[0], T, dividends=False, now=NOW)
    last = result.positions[result.positions["date"].isna()]
    assert plan.targets == {r.symbol: r.target_weight for r in last.itertuples() if r.target_weight}
    prices = backtest.prepare(data, dividends=False)
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


def test_duplicate_id_counts_as_sent(env):
    plan = env.run()
    env.broker.orders = [order(oid(1, "SPY"), "new")]  # it reached Alpaca before
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
