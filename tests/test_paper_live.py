"""Live checks of Alpaca paper behaviour the paper-trading design relies on.

They record facts (printed with -s) more than they assert. Every order uses a
client_order_id starting with "sb-test-" and is cancelled afterwards.
"""
import os
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import CorporateActionsType
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.requests import CorporateActionsRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetCalendarRequest, GetOrdersRequest, GetPortfolioHistoryRequest,
                                     MarketOrderRequest)

from app.settings import load_keys

pytestmark = pytest.mark.skipif(os.environ.get("STOCKBARS_LIVE") != "1", reason="needs a real key")

NY = ZoneInfo("America/New_York")
PREFIX = "sb-test-"
RUN = uuid.uuid4().hex[:8]


def cid(name):
    return f"{PREFIX}{RUN}-{name}"[:48]


def note(*a):
    print("\nFACT:", *a)


def err(e):
    return f"{type(e).__name__}: {e}"


def open_test_orders(client):
    orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
    return [o for o in orders if (o.client_order_id or "").startswith(PREFIX)]


@pytest.fixture(scope="module")
def client():
    keys = load_keys()
    assert keys, "no stored Alpaca key"
    c = TradingClient(*keys, paper=True)
    note("ET now", datetime.now(NY).isoformat(timespec="seconds"))
    yield c
    for o in open_test_orders(c):
        try:
            c.cancel_order_by_id(o.id)
        except APIError as e:
            print("cleanup failed for", o.client_order_id, err(e))
    assert not open_test_orders(c)


def opg(symbol="SPY", qty=1, side=OrderSide.BUY, client_order_id=None):
    return MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.OPG,
                              client_order_id=client_order_id)


def test_account_is_paper(client):
    a = client.get_account()
    note("account_number", a.account_number, "status", a.status, "equity", a.equity, "cash", a.cash,
         "buying_power", a.buying_power, "multiplier", a.multiplier, "last_equity", a.last_equity)
    assert a.account_number.startswith("P")


def test_calendar_lists_sessions(client):
    today = date.today()
    cal = client.get_calendar(GetCalendarRequest(start=today - timedelta(days=10), end=today))
    days = [c.date for c in cal]
    note("calendar days", [d.isoformat() for d in days], "first", cal[0].open, cal[0].close)
    missing = [(today - timedelta(days=i)) for i in range(11) if (today - timedelta(days=i)) not in days]
    note("days with no session", [d.isoformat() for d in missing])
    assert days and all(d.weekday() < 5 for d in days) and missing


def test_opg_order_by_client_id(client):
    i = cid("a")
    try:
        o = client.submit_order(opg(client_order_id=i))
    except APIError as e:
        note("OPG submit rejected at", datetime.now(NY).isoformat(timespec="seconds"), err(e))
        pytest.fail("OPG rejected, see FACT line")
    note("OPG accepted", o.status, o.time_in_force, "at", datetime.now(NY).isoformat(timespec="seconds"))
    got = client.get_order_by_client_id(i)
    assert got.id == o.id
    client.cancel_order_by_id(o.id)
    after = client.get_order_by_client_id(i)
    note("status right after cancel", after.status)
    for _ in range(10):
        after = client.get_order_by_client_id(i)
        if "cancel" in str(after.status).lower():
            break
    note("final status", after.status)
    assert "cancel" in str(after.status).lower()


def test_client_id_reuse_after_cancel(client):
    i = cid("b")
    o = client.submit_order(opg(client_order_id=i))
    client.cancel_order_by_id(o.id)
    try:
        o2 = client.submit_order(opg(client_order_id=i))
        note("reuse after cancel ACCEPTED", o2.id == o.id, o2.status)
    except APIError as e:
        note("reuse after cancel REJECTED", err(e), "status_code", e.status_code)


def test_duplicate_client_id_error_text(client):
    i = cid("c")
    client.submit_order(opg(client_order_id=i))
    try:
        client.submit_order(opg(client_order_id=i))
        note("duplicate ACCEPTED")
    except APIError as e:
        note("duplicate REJECTED", err(e), "status_code", e.status_code, "code", getattr(e, "code", None))


def test_fractional_opg(client):
    i = cid("d")
    try:
        o = client.submit_order(opg(qty=0.5, client_order_id=i))
        note("fractional OPG ACCEPTED", o.status, o.qty)
    except APIError as e:
        note("fractional OPG REJECTED", err(e), "status_code", e.status_code)


def test_list_orders_by_day(client):
    start = datetime.now(NY).replace(hour=0, minute=0, second=0, microsecond=0)
    orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, after=start, limit=500))
    mine = [o for o in orders if (o.client_order_id or "").startswith(f"{PREFIX}{RUN}")]
    note("orders today", len(orders), "of this run", sorted(o.client_order_id for o in mine))
    assert mine


def test_portfolio_history_daily(client):
    h = client.get_portfolio_history(GetPortfolioHistoryRequest(period="1M", timeframe="1D"))
    note("history timestamps", [datetime.fromtimestamp(t, NY).isoformat(timespec="minutes") for t in h.timestamp])
    note("history equity", h.equity, "base_value", h.base_value, "timeframe", h.timeframe)
    note("history profit_loss", h.profit_loss)
    assert len(h.timestamp) == len(h.equity)


def test_corporate_actions_reach_next_day():
    c = CorporateActionsClient(*load_keys())
    today = date.today()
    try:
        r = c.get_corporate_actions(CorporateActionsRequest(
            symbols=["SPY", "AAPL"], start=today - timedelta(days=5), end=today + timedelta(days=1),
            types=[CorporateActionsType.FORWARD_SPLIT, CorporateActionsType.REVERSE_SPLIT], limit=None))
        note("end=tomorrow accepted", dict(r.data))
    except Exception as e:
        note("end=tomorrow REJECTED", err(e))
        raise
    try:
        r = c.get_corporate_actions(CorporateActionsRequest(
            start=today, end=today + timedelta(days=60),
            types=[CorporateActionsType.FORWARD_SPLIT, CorporateActionsType.REVERSE_SPLIT], limit=50))
        splits = {k: [(a.symbol, str(a.ex_date)) for a in v] for k, v in r.data.items()}
        note("announced future splits next 60 days (all symbols)", splits or "no case available")
    except Exception as e:
        note("future split sweep failed", err(e))


def test_buying_power_with_pending_sale(client):
    note("buying_power", client.get_account().buying_power, "positions", len(client.get_all_positions()))
    pytest.skip("not checked: pending-sale buying power needs a held SPY position; "
                "selling is not allowed in this check")


def test_paper_day_end_to_end(tmp_path):
    """Run window only: prepare, send, kill, release one SPY share. Temp journal and a temp copy of bars.db."""
    import shutil
    from pathlib import Path

    from alpaca.data.historical import StockHistoricalDataClient
    from platformdirs import user_data_dir

    from app.paper import (Plan, account_key, kill, make_broker, prepare_plan, release, send_plan)
    from app.paper_plan import Limits, run_window
    from app.paper_state import AccountStore
    from app.store import Store

    keys = load_keys()
    assert keys, "no stored Alpaca key"
    broker = make_broker(keys)
    now = datetime.now(NY)
    today = now.date()
    if why := run_window(broker.calendar(today - timedelta(days=10), today + timedelta(days=10)), now)[2]:
        pytest.skip(f"run window is closed: {why}")

    real = Path(user_data_dir("stockapp", appauthor=False)) / "bars.db"
    for suffix in ("", "-wal", "-shm"):
        if Path(f"{real}{suffix}").exists():
            shutil.copy(f"{real}{suffix}", tmp_path / f"bars.db{suffix}")
    number = broker.account().number
    before = {o.client_order_id for o in broker.open_orders()}
    acct = AccountStore(tmp_path / "paper", account_key(broker.account()))
    acct.save_settings(strategy_path="tests/fixtures/buy_spy.py", limits=Limits(max_weight=0.01))
    store = Store(str(tmp_path / "bars.db"))
    try:
        plan = prepare_plan(broker, acct, store, StockHistoricalDataClient(*keys), broker.actions, now)
        assert isinstance(plan, Plan), plan
        assert [(o.symbol, o.side) for o in plan.orders] == [("SPY", "buy")] and plan.orders[0].qty >= 1
        res = send_plan(broker, acct, plan, datetime.now(NY))
        assert not res.failed and res.discarded is None and [o.client_order_id for o in res.sent] == [
            plan.orders[0].client_order_id]
        assert res.sent[0].status in ("accepted", "new", "pending_new")
        kill(broker, acct)
        j = acct.journal(plan.day, plan.attempt)
        cid = plan.orders[0].client_order_id
        assert broker.order(cid).status == "canceled"
        assert release(broker, acct) is None
        seen = [e["status"] for e in j.events() if e.get("id") == cid]
        assert seen[0] == "sending" and seen[-1] == "canceled"
        assert any(st in ("accepted", "new", "pending_new") for st in seen[1:-1])
        assert j.closed() == "killed"
    finally:
        for o in broker.open_orders():  # never leave an order open, even when an assert failed
            if o.client_order_id not in before and (o.client_order_id or "").startswith(f"sb-{number}-"):
                try:
                    broker.trading.cancel_order_by_id(o.id)
                except Exception as e:  # keep the original failure and cancel the rest
                    print("cleanup failed for", o.client_order_id, err(e))
        store.db.close()
