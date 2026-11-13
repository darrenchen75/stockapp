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
from app.paper_state import AccountStore
from app.store import ET, DailyBar, Store
from tests.paper_fakes import FakeBroker
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
