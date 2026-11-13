"""The paper-trading daily step: the Alpaca broker wrapper, preparing, sending and recovering a plan, the kill
switch and selling everything. No Qt imports."""
import hashlib
import json
import math
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from time import sleep as _sleep

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import CorporateActionsType
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.requests import CorporateActionsRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetCalendarRequest, GetOrdersRequest, GetPortfolioHistoryRequest,
                                     MarketOrderRequest)

from app.backtest import StrategyError, decide, history_as_of, input_hash, load_strategy, prepare, rebalance
from app.daily import ET, decision_time, load_bars, refresh_daily, universe
from app.paper_plan import (SEND_DEADLINE, Limits, LossCheck, Note, Order, check_losses, order_id, plan_orders,
                             run_window)
from app.paper_state import AccountStore, Journal, _write_json
from app.store import Store


@dataclass(frozen=True)
class Account:
    number: str
    endpoint: str
    equity: float
    cash: float
    buying_power: float


@dataclass(frozen=True)
class BrokerOrder:
    client_order_id: str
    id: str
    symbol: str
    side: str
    qty: float
    filled_qty: float
    filled_avg_price: float | None
    status: str


def _order(o) -> BrokerOrder:
    value = lambda x: getattr(x, "value", x)  # alpaca-py enums to plain strings
    return BrokerOrder(o.client_order_id, str(o.id), o.symbol, value(o.side), float(o.qty or 0),
                       float(o.filled_qty or 0), None if o.filled_avg_price is None else float(o.filled_avg_price),
                       value(o.status))


class Broker:
    """Alpaca's trading and corporate-actions clients, answering in this module's dataclasses."""

    def __init__(self, trading, actions):
        self.trading, self.actions = trading, actions

    def account(self) -> Account:
        a, url = self.trading.get_account(), self.trading._base_url
        return Account(a.account_number, str(getattr(url, "value", url)), float(a.equity), float(a.cash),
                       float(a.buying_power))

    def calendar(self, start: date, end: date) -> list[date]:
        return [c.date for c in self.trading.get_calendar(GetCalendarRequest(start=start, end=end))]

    def positions(self) -> dict[str, float]:
        return {p.symbol: float(p.qty) for p in self.trading.get_all_positions()}

    def orders_since(self, after: datetime) -> list[BrokerOrder]:
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, after=after, limit=500)
        return [_order(o) for o in self.trading.get_orders(req)]

    def open_orders(self) -> list[BrokerOrder]:
        return [_order(o) for o in self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))]

    def order(self, client_order_id: str) -> BrokerOrder | None:
        try:
            return _order(self.trading.get_order_by_client_id(client_order_id))
        except APIError as e:
            if e.status_code == 404:
                return None
            raise

    def submit(self, order: Order, tif: str = "opg") -> BrokerOrder:
        return _order(self.trading.submit_order(MarketOrderRequest(
            symbol=order.symbol, qty=order.qty, side=OrderSide(order.side), time_in_force=TimeInForce(tif),
            client_order_id=order.client_order_id)))

    def cancel_all(self) -> list[tuple[str, bool]]:
        return [(str(r.id), 200 <= r.status < 300) for r in self.trading.cancel_orders()]

    def equity_history(self) -> list[tuple[date, float]]:
        h = self.trading.get_portfolio_history(GetPortfolioHistoryRequest(period="1A", timeframe="1D"))
        # a missing value counts as 0, which the loss check treats as a problem that blocks buys
        return [(datetime.fromtimestamp(t, ET).date(), float(e or 0.0)) for t, e in zip(h.timestamp, h.equity)]

    def splits(self, symbols: list[str], start: date, end: date) -> dict[str, date]:
        data = self.actions.get_corporate_actions(CorporateActionsRequest(
            symbols=symbols, start=start, end=end, limit=None,
            types=[CorporateActionsType.FORWARD_SPLIT, CorporateActionsType.REVERSE_SPLIT])).data
        return {a.symbol: a.ex_date for key in ("forward_splits", "reverse_splits") for a in data.get(key, [])}

    def market_open(self) -> bool:
        return self.trading.get_clock().is_open


def make_broker(keys: tuple[str, str]) -> Broker:
    return Broker(TradingClient(*keys, paper=True), CorporateActionsClient(*keys))  # never live


def account_key(account: Account) -> str:
    return f"paper-{account.number}"


@dataclass(frozen=True)
class Refusal:
    reason: str


@dataclass(frozen=True)
class Plan:
    account_number: str
    day: date
    next_day: date
    attempt: int
    orders: list[Order]
    notes: list[Note]
    targets: dict[str, float]
    input_hash: str
    data_date: str
    downloaded_at: str
    account: Account
    positions: dict[str, float]
    limits: Limits
    strategy_sha256: str
    dividends: bool
    loss: LossCheck
    loss_lock: dict | None  # the account's lock when the plan was made; sending rechecks it
    last_bars: dict[str, dict]  # day-T bar per held or targeted symbol
    sell_all: bool = False  # a sell-everything plan, sent under the halt by send_sell_all


def _date(iso: str | None) -> date | None:
    return date.fromisoformat(iso) if iso else None


def plan_to_json(plan: Plan) -> dict:
    return json.loads(json.dumps(asdict(plan), default=date.isoformat))


def plan_from_json(d: dict) -> Plan:
    loss = d["loss"]
    return Plan(**{**d, "day": date.fromisoformat(d["day"]), "next_day": date.fromisoformat(d["next_day"]),
                   "orders": [Order(**o) for o in d["orders"]], "notes": [Note(**n) for n in d["notes"]],
                   "account": Account(**d["account"]), "limits": Limits(**d["limits"]),
                   "loss": LossCheck([(date.fromisoformat(s), x) for s, x in loss["breaches"]],
                                     _date(loss["checked_through"]), loss["problem"])})


_UNSENT = {None, "not_found"}  # journal states of an order that never reached Alpaca
_NO_RESULT = _UNSENT | {"sending"}


def _remainder(acct: AccountStore, day: date, attempts: list[int], sell_all: bool) -> Plan | None:
    """The latest attempt's saved plan, cut to its unsent orders, if that attempt is still open, is of the kind
    asked for and a send started (step 11 journals "sending" first). An attempt never sent is recomputed instead."""
    if not attempts:
        return None
    j = acct.journal(day, attempts[-1])
    try:
        saved = plan_from_json(j.plan())
    except FileNotFoundError:  # a crash before plan.json was written
        return None
    sent = j.order_states()
    unsent = [o for o in saved.orders if sent.get(o.client_order_id) in _UNSENT]
    return replace(saved, orders=unsent) if sent and unsent and not j.closed() and saved.sell_all == sell_all else None


def _load_exactly(code: bytes, path: str):
    """The strategy from these bytes, not from the file again, so what runs is what is saved and hashed."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "strategy.py"
        copy.write_bytes(code)
        try:
            return load_strategy(str(copy))
        except StrategyError as e:
            raise StrategyError(str(e).replace(str(copy), path)) from e


def _attempt(broker: Broker, acct: AccountStore, number: str, day: date,
             sell_all: bool) -> Refusal | tuple[Plan | None, int, list[Note]]:
    """(the open attempt's unsent remainder or None, the next attempt number, notes), or why there is none:
    an order from another day or attempt is still open. The number is 1 + the highest here or at Alpaca."""
    prefix = f"sb-{number}-{day:%Y%m%d}-a"
    ours = re.compile(rf"{re.escape(prefix)}(\d+)-")
    remote = {int(m[1]) for o in broker.orders_since(datetime.combine(day, time(), ET))
              if (m := ours.match(o.client_order_id or ""))}
    local = acct.attempts(day)
    left = _remainder(acct, day, local, sell_all)
    mine = f"{prefix}{left.attempt}-" if left else None
    blocking = [o.client_order_id for o in broker.open_orders()
                if not (mine and (o.client_order_id or "").startswith(mine))]
    if blocking:
        return Refusal(f"{len(blocking)} open order(s) from another day or attempt, such as {blocking[0]}. "
                       "Wait for them to finish or cancel them.")
    notes = []
    if remote - set(local):
        notes.append(Note(None, "warning", f"Alpaca has orders for {day} that this computer has no journal "
                                           "for: another computer may be running this account."))
    return left, max([*local, *remote], default=0) + 1, notes


def _journal_plan(acct: AccountStore, plan: Plan) -> Plan:
    """Step 8: save the plan before anyone can confirm it."""
    journal = acct.journal(plan.day, plan.attempt)
    journal.write_plan(plan_to_json(plan))
    for n in acct.attempts(plan.day):  # earlier attempts nobody started sending are replaced by this one
        old = acct.journal(plan.day, n)
        if n < plan.attempt and not old.closed() and not old.order_states():
            old.close("superseded")
    return plan


def prepare_plan(broker: Broker, acct: AccountStore, store: Store, data_client, actions_client,
                 now: datetime) -> Plan | Refusal:
    """The spec's daily steps 1-8: a journaled plan ready to confirm, or why there is none."""
    with acct.lock():
        settings, state = acct.settings(), acct.state()
        if state["halted"]:
            return Refusal("The account is halted. Release the halt before running.")
        account = broker.account()
        if account_key(account) != acct.dir.name:
            return Refusal(f"The keys point to account {account.number}, not {acct.dir.name}.")
        today = now.astimezone(ET).date()
        day, next_day, why = run_window(broker.calendar(today - timedelta(days=10), today + timedelta(days=10)), now)
        if why:
            return Refusal(why)

        got = _attempt(broker, acct, account.number, day, sell_all=False)
        if isinstance(got, Refusal):
            return got
        left, attempt, notes = got
        if left:
            return left  # never recomputed: same IDs and quantities as confirmed

        path = settings["strategy_path"]
        if not path:
            return Refusal("No strategy file is chosen.")
        if not Path(path).is_file():
            return Refusal(f"Strategy file not found: {path}")
        code = Path(path).read_bytes()
        try:
            strategy = _load_exactly(code, path)
        except StrategyError as e:
            return Refusal(str(e))

        # step 2: data through day T, verified by SPY's last bar
        symbols = universe()
        try:
            refresh_daily(data_client, actions_client, store, symbols, now=now, expect=day)
        except Exception as e:
            return Refusal(f"Could not download daily data through {day}: {e}")
        prices = prepare(load_bars(store, now, symbols), settings["dividends"])
        stamp = pd.Timestamp(day)
        closes = {s: float(c) for s, c in prices.raw["c"].loc[stamp].dropna().items()}
        if missing := sorted(set(symbols) - set(closes)):
            notes.append(Note(None, "warning", f"No bar for {day} for {len(missing)} symbol(s): {', '.join(missing)}"))

        # step 3: the strategy, through the backtest's own decision step
        history = history_as_of(prices, stamp)
        try:
            targets = decide(strategy, history, stamp, set(prices.raw["c"].columns))
        except StrategyError as e:
            return Refusal(str(e))

        # step 4: the account and the loss cutoff over every session since the last one checked
        positions = broker.positions()
        limits = settings["limits"]
        equity = [p for p in broker.equity_history() if p[0] <= day]  # a morning run may see T+1's point
        loss = check_losses(equity, _date(state["loss_checked_through"]), limits.loss_cutoff)
        last = max(equity, default=None)  # (date, equity) of the latest history point
        live = not loss.problem and last and last[0] < day and last[1] > 0
        if live:  # Alpaca's history lacks T yet: after 20:30 the account's equity is T's close
            change = account.equity / last[1] - 1
            if change < -limits.loss_cutoff:
                loss = replace(loss, breaches=[*loss.breaches, (day, change)])
        lock = state["loss_lock"]  # only a person clears it
        fresh = [(d, x) for d, x in loss.breaches if d.isoformat() not in state["cleared"]]
        problem_day = (loss.checked_through or day).isoformat()
        if fresh:
            lock = {"session": fresh[-1][0].isoformat(), "loss": fresh[-1][1], "problem": None}
        elif loss.problem and problem_day not in state["cleared"]:
            lock = {"session": problem_day, "loss": None, "problem": loss.problem}
        through = loss.checked_through.isoformat() if loss.checked_through else state["loss_checked_through"]
        if (through, lock) != (state["loss_checked_through"], state["loss_lock"]):
            acct.save_state(loss_checked_through=through, loss_lock=lock)
        if live:
            notes.append(Note(None, "warning", f"Portfolio history reaches only {last[0]}, so the loss on {day} "
                                               "was checked from live equity; the next run checks it again."))

        # step 5: splits taking effect at T+1's open; no answer never counts as no split
        wanted = sorted(set(targets) | {s for s, q in positions.items() if q})
        try:
            split = broker.splits(wanted, day + timedelta(days=1), next_day) if wanted else {}
        except Exception as e:
            return Refusal(f"Could not check for splits through {next_day}: {e}")

        # steps 6-7: orders under the safety limits
        orders, more = plan_orders(targets, positions, account.equity, closes, limits, lock is not None, set(split),
                                   account.number, day, attempt)
        last_bars = {s: {k: float(prices.raw[k].at[stamp, s]) for k in ("o", "h", "l", "c", "v")}
                     for s in wanted if s in closes}
        plan = Plan(account.number, day, next_day, attempt, orders, more + notes, targets, input_hash(history),
                    store.daily_last("SPY"), now.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    account, positions, limits, hashlib.sha256(code).hexdigest(), settings["dividends"], loss,
                    lock, last_bars)

        (acct.journal(day, attempt).folder / "strategy.py").write_bytes(code)
        return _journal_plan(acct, plan)


@dataclass(frozen=True)
class SendResult:
    sent: list[BrokerOrder]
    failed: list[tuple[str, str]]  # (client_order_id, Alpaca's error)
    discarded: str | None  # why the recheck threw the plan away


def _close_if_sent(j: Journal) -> None:
    states = j.order_states()
    if not j.closed() and all(states.get(o["client_order_id"]) not in _NO_RESULT for o in j.plan()["orders"]):
        j.close("sent")


_WINDOW_CLOSED = "the at-the-open window has closed; wait for the market to open"


def _recheck(broker: Broker, acct: AccountStore, plan: Plan, now: datetime, tif: str) -> str | None:
    """Step 10: why the plan no longer holds, or None."""
    if tif == "opg" and now >= datetime.combine(plan.next_day, SEND_DEADLINE, ET):
        return _WINDOW_CLOSED if plan.sell_all else f"The 09:28 New York send deadline for {plan.next_day} has passed."
    state = acct.state()
    if state["halted"] and not plan.sell_all:  # selling everything requires the halt
        return "The account is halted."
    if state["loss_lock"] != plan.loss_lock:
        return "The loss lock changed since the plan was made."
    if acct.settings()["limits"] != plan.limits:
        return "The safety settings changed since the plan was made."
    account = broker.account()
    if (account.number, account.endpoint) != (plan.account_number, plan.account.endpoint):
        return f"The keys now point to account {account.number} at {account.endpoint}, not the plan's."
    mine = f"sb-{plan.account_number}-{plan.day:%Y%m%d}-a{plan.attempt}-"
    if other := [o.client_order_id for o in broker.open_orders() if not (o.client_order_id or "").startswith(mine)]:
        return f"{len(other)} open order(s) from another day or attempt, such as {other[0]}."
    held = broker.positions()
    if held.keys() != plan.positions.keys() or any(abs(q - plan.positions[s]) > 1e-9 for s, q in held.items()):
        return "Positions changed since the plan was made."
    return None


_NO_ANSWER = "no answer from Alpaca; will be checked on next open"


def _look_up(broker: Broker, cid: str) -> BrokerOrder | None:
    """The order at Alpaca, or None when Alpaca doesn't know it or the lookup itself fails."""
    try:
        return broker.order(cid)
    except Exception:
        return None


def send_plan(broker: Broker, acct: AccountStore, plan: Plan, now: datetime) -> SendResult:
    """Steps 10-11: recheck, then send the confirmed orders, journaling each before and after its request."""
    return _send(broker, acct, plan, now, sell_all=False)


def send_sell_all(broker: Broker, acct: AccountStore, plan: Plan, now: datetime) -> SendResult:
    """Send a sell-everything plan like send_plan, under the halt: as day orders while the market is open, else
    at the open."""
    return _send(broker, acct, plan, now, sell_all=True)


def _send(broker: Broker, acct: AccountStore, plan: Plan, now: datetime, sell_all: bool) -> SendResult:
    if plan.sell_all != sell_all:
        raise ValueError("send a sell-everything plan with send_sell_all and a daily plan with send_plan")
    if plan.sell_all and any(o.side != "sell" or o.qty <= 0 for o in plan.orders):
        raise ValueError("a sell-everything plan may hold only sales")  # it is sent under the halt
    with acct.lock():
        killed = acct.state()["killed_at"]  # a sell-all runs halted, so a kill shows as a new kill time
        j = acct.journal(plan.day, plan.attempt)
        if why := j.closed():
            return SendResult([], [], f"This attempt is already closed ({why}). Run again for a new plan.")
        for cid in [c for c, st in j.order_states().items() if st == "sending"]:  # looked up before anything goes
            if not (got := _look_up(broker, cid)):
                return SendResult([], [(cid, _NO_ANSWER)], None)
            j.mark(cid, got.status, asdict(got))
        tif = "day" if plan.sell_all and broker.market_open() else "opg"
        if why := _recheck(broker, acct, plan, now, tif):
            if all(st in _UNSENT for st in j.order_states().values()):  # nothing of it is at Alpaca
                j.close("discarded")
            return SendResult([], [], why)
        sent, failed, states = [], [], j.order_states()
        for o in plan.orders:
            cid = o.client_order_id
            if states.get(cid) not in _UNSENT:  # already answered by Alpaca
                continue
            state = acct.state()
            if (state["killed_at"] != killed) if plan.sell_all else state["halted"]:
                break
            j.mark(cid, "sending")
            try:
                got = broker.submit(o, tif)
            except Exception as e:
                status = e.status_code if isinstance(e, APIError) else None
                if "client_order_id must be unique" in str(e):
                    got = broker.order(cid)  # it reached Alpaca already, from an earlier send or another computer
                    if got is None:
                        j.mark(cid, "not_found")
                        continue
                elif status is not None and 400 <= status < 500:  # Alpaca answered: a rejection
                    j.mark(cid, "failed", {"error": str(e)})  # str(APIError) is Alpaca's response body
                    failed.append((cid, str(e)))
                    break  # the rest is offered again once the error has been seen
                elif not (got := _look_up(broker, cid)):  # no answer, or a 5xx: it may have reached Alpaca
                    failed.append((cid, _NO_ANSWER))  # left at "sending" for recover() on the next open
                    break
            j.mark(cid, got.status, asdict(got))
            if got.status == "rejected":
                failed.append((cid, got.status))
                break
            sent.append(got)
        _close_if_sent(j)
        return SendResult(sent, failed, None)


def recover(broker: Broker, acct: AccountStore) -> list[str]:
    """Look up every order a crash left "sending" and record its real state, before anything else is sent."""
    with acct.lock():
        lines = []
        for j, cid in acct.unresolved():
            got = broker.order(cid)
            if got:
                j.mark(cid, got.status, asdict(got))
                lines.append(f"{cid}: {got.status}")
            else:
                j.mark(cid, "not_found")
                lines.append(f"{cid}: not found at Alpaca, so it is offered again")
            _close_if_sent(j)
        return lines


_FINAL = {"canceled", "filled", "expired", "rejected", "failed", "not_found"}  # Alpaca has nothing open for these
_POLL = 0.5  # seconds between status checks after a cancel
_RECANCELS = 3  # extra cancel_all calls a kill makes while an order is still unfinished


def _settle(broker: Broker, acct: AccountStore, polls: int = 0, sleep=_sleep, close: bool = False,
            recancel: int = 0) -> dict[str, str]:
    """The latest status of every order of every open attempt and of every order Alpaca still has open. Orders
    not finished are looked up again, up to `polls` more times (cancelling everything again before the first
    `recancel` of them), and new statuses are journaled. With `close`, attempts whose orders are all finished
    are closed "killed"."""
    for n in range(polls + 1):
        if n:
            if n <= recancel:  # an order may have got out after the last cancel
                broker.cancel_all()
            sleep(_POLL)
        live = {o.client_order_id or o.id: o for o in broker.open_orders()}
        status = {cid: o.status for cid, o in live.items()}
        journals = [j for j in acct.entries() if not j.closed()]
        for j in journals:
            for cid, st in j.order_states().items():
                got, new = live.get(cid), st
                if got:  # Alpaca lists it open, whatever the journal says
                    new = got.status
                elif st not in _FINAL:
                    with suppress(Exception):  # no answer leaves it unconfirmed
                        got = broker.order(cid)
                        # a "sending" order Alpaca doesn't know may still arrive; recover() settles it later
                        new = got.status if got else st if st == "sending" else "not_found"
                if new != st:
                    j.mark(cid, new, got and asdict(got))
                status[cid] = new
        if all(st in _FINAL for st in status.values()):
            break
    if close:
        for j in journals:
            if all(st in _FINAL for st in j.order_states().values()):
                j.close("killed")
    return status


def kill(broker: Broker, acct: AccountStore, timeout: float = 5.0, sleep=_sleep) -> list[tuple[str, str]]:
    """Halt, cancel every open order, then confirm each one finished for up to `timeout` seconds, cancelling
    again while any isn't, and close the attempts that are. Takes no account lock: it must work while a send
    holds it."""
    acct.save_state(halted=True, killed_at=datetime.now(timezone.utc).isoformat())  # on disk first
    broker.cancel_all()
    return list(_settle(broker, acct, round(timeout / _POLL), sleep, close=True, recancel=_RECANCELS).items())


def unresolved_cancels(broker: Broker, acct: AccountStore) -> list[str]:
    """IDs of orders Alpaca hasn't yet reported cancelled, filled or rejected."""
    return [cid for cid, st in _settle(broker, acct).items() if st not in _FINAL]


def release(broker: Broker, acct: AccountStore) -> str | None:
    """Lift the halt, closing the killed attempts, once every order is finished; else why not."""
    if not acct.state()["halted"]:
        return None
    if left := [cid for cid, st in _settle(broker, acct, close=True).items() if st not in _FINAL]:
        return (f"{len(left)} order(s) not yet confirmed cancelled, filled or rejected, such as {left[0]}. "
                "Kill again or wait for them to finish.")
    acct.save_state(halted=False)
    return None


def plan_sell_all(broker: Broker, acct: AccountStore, now: datetime) -> Plan | Refusal:
    """A journaled plan selling every whole share held, under the halt, once no other order is open."""
    with acct.lock():
        account = broker.account()
        if account_key(account) != acct.dir.name:
            return Refusal(f"The keys point to account {account.number}, not {acct.dir.name}.")
        today = now.astimezone(ET).date()
        day, next_day, why = run_window(broker.calendar(today - timedelta(days=10), today + timedelta(days=10)), now)
        if day == next_day:  # no session to sell in; a passed 09:28 deadline still leaves market hours
            return Refusal(why)
        got = _attempt(broker, acct, account.number, day, sell_all=True)
        if isinstance(got, Refusal):
            return got
        left, attempt, notes = got
        state = acct.state()
        if not state["halted"]:
            acct.save_state(halted=True)
        if left:
            return left  # never recomputed: same IDs and quantities as confirmed
        positions, settings, orders = broker.positions(), acct.settings(), []
        for s, q in sorted(positions.items()):
            whole = math.floor(q)
            if q != whole:
                notes.append(Note(s, "fractional", f"{s}: {q - whole:.4f} of a share is left alone"))
            if whole > 0:
                orders.append(Order(s, "sell", whole, order_id(account.number, day, attempt, s), math.nan))
        plan = Plan(account.number, day, next_day, attempt, orders, notes, {}, "", "",
                    now.astimezone(timezone.utc).isoformat(timespec="seconds"), account, positions,
                    settings["limits"], "", settings["dividends"],
                    LossCheck([], _date(state["loss_checked_through"]), None), state["loss_lock"], {}, sell_all=True)
        return _journal_plan(acct, plan)


# --- fills, drift reasons, replay, export ---

FILL_COLUMNS = ["day", "symbol", "side", "ordered", "filled", "avg_price", "open_next", "close_t", "bps_vs_open",
                "bps_vs_close", "backtest_shares", "planned_shares", "sent_shares", "filled_shares", "reasons"]
_TEXT = {"day", "symbol", "side", "reasons"}
_NAN = math.nan


def _bps(side: str, avg: float, ref: float) -> float:
    """Signed so a positive number is a cost: paid more on a buy, got less on a sale."""
    return (avg - ref) / ref * 10_000 * (1 if side == "buy" else -1)


def _sent(j: Journal) -> dict[str, str]:
    return {c: st for c, st in j.order_states().items() if st not in _NO_RESULT}


def _fill_rows(broker: Broker, j: Journal, plan: Plan, store: Store) -> tuple[list[dict], bool]:
    """(rows, final) for one attempt. final: T+1's daily bar is in the store, so the row never changes again."""
    reached = {c: st for c, st in _sent(j).items() if st != "failed"}
    if not reached:
        return [], False
    wanted = sorted({*plan.positions, *plan.targets, *(o.symbol for o in plan.orders), "SPY"})
    nxt = load_bars(store, decision_time(plan.next_day), wanted)
    nxt = nxt[nxt["date"] == pd.Timestamp(plan.next_day)]
    final = "SPY" in set(nxt["symbol"])
    opens = {r.symbol: float(r.o) for r in nxt.itertuples()}
    closes = {s: b["c"] for s, b in plan.last_bars.items()}
    try:  # what the backtest's rules would hold after T+1's open, with no costs
        want = rebalance(plan.positions, plan.account.cash, plan.targets, opens, closes, 0.0).shares if final else {}
    except (KeyError, ValueError):
        want = {}
    by_symbol = {o.symbol: o for o in plan.orders}
    blocked = {n.symbol: n for n in plan.notes if n.kind in ("blocked_loss", "blocked_split") and n.symbol}
    rows = []
    for s in sorted(by_symbol.keys() | blocked.keys()):
        o, held = by_symbol.get(s), plan.positions.get(s, 0.0)
        sign = 1 if o and o.side == "buy" else -1
        got = broker.order(o.client_order_id) if o and o.client_order_id in reached else None
        filled = got.filled_qty if got else 0.0
        avg = got.filled_avg_price if got and got.filled_avg_price else _NAN
        open_next, close_t = opens.get(s, _NAN), closes.get(s, _NAN)
        kinds = {n.kind for n in plan.notes if n.symbol == s}
        reasons = [r for k, r in (("trim_cap", "size_cap"), ("blocked_loss", "loss_lock"),
                                  ("blocked_split", "split_block")) if k in kinds]
        if (w := plan.targets.get(s)) and close_t > 0:
            exact = w * plan.account.equity / close_t
            if math.floor(w * plan.account.equity * (1 - plan.limits.cash_buffer) / close_t) < math.floor(exact + 1e-9):
                reasons.append("cash_buffer")
            if abs(exact - round(exact)) > 1e-9:
                reasons.append("rounding")
        if o and filled < o.qty:
            reasons.append("unfilled")
        rows.append(dict(
            day=plan.day.isoformat(), symbol=s, side=o.side if o else "", ordered=o.qty if o else 0.0,
            filled=filled, avg_price=avg, open_next=open_next, close_t=close_t,
            bps_vs_open=_bps(o.side, avg, open_next) if o and open_next > 0 and avg == avg else _NAN,
            bps_vs_close=_bps(o.side, avg, close_t) if o and close_t > 0 and avg == avg else _NAN,
            backtest_shares=want.get(s, 0.0) if final and want else _NAN,
            planned_shares=held + (sign * o.qty if o else 0.0),
            sent_shares=held + (sign * o.qty if o and o.client_order_id in reached else 0.0),
            filled_shares=held + sign * filled, reasons=";".join(reasons)))
    return rows, final


def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=FILL_COLUMNS)
    for c in FILL_COLUMNS:
        df[c] = df[c].astype(object).where(df[c].notna(), None) if c in _TEXT else pd.to_numeric(df[c])
    return df


def track_fills(broker: Broker, acct: AccountStore, store: Store) -> pd.DataFrame:
    """One row per order sent (and per buy a limit blocked), oldest day first. A row is saved in its attempt's
    fills.json and kept as is once T+1's daily bar exists; before that it is recomputed on each call."""
    rows = []
    for j in reversed(acct.entries()):
        path = j.folder / "fills.json"
        saved = json.loads(path.read_text()) if path.exists() else None
        if saved and saved["final"]:
            rows += saved["rows"]
            continue
        try:
            plan = plan_from_json(j.plan())
        except FileNotFoundError:
            continue
        got, final = _fill_rows(broker, j, plan, store)
        if got:
            clean = [{k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in r.items()} for r in got]
            _write_json(path, {"final": final, "rows": clean})
            rows += clean
    return _frame(rows)


def cost_summary(fills: pd.DataFrame) -> dict[str, float]:
    """Averages of bps_vs_open and bps_vs_close weighted by traded value (nan when nothing was traded)."""
    value = fills["filled"] * fills["avg_price"]
    out = {}
    for col in ("bps_vs_open", "bps_vs_close"):
        ok = fills[col].notna() & (value > 0)
        out[col] = float((fills[col][ok] * value[ok]).sum() / value[ok].sum()) if ok.any() else _NAN
    return out


def negative_cash_days(acct: AccountStore, broker: Broker) -> list[tuple[date, float]]:
    """Days a plan's account snapshot (taken after the close) showed cash below 0, plus today if it is negative now."""
    found = {}
    for j in reversed(acct.entries()):  # oldest first, so the latest attempt of a day wins
        with suppress(FileNotFoundError):
            plan = j.plan()
            found[date.fromisoformat(plan["day"])] = plan["account"]["cash"]
    found[datetime.now(ET).date()] = broker.account().cash
    return sorted((d, c) for d, c in found.items() if c < 0)


def missed_sessions(acct: AccountStore, calendar: list[date], now: datetime | None = None) -> list[date]:
    """Sessions from the first sent attempt on, whose send deadline has passed, with no order that has a journal result."""
    now = now or datetime.now(ET)
    sent = {date.fromisoformat(j.plan()["day"]) for j in acct.entries() if _sent(j)}
    if not sent:
        return []
    days = sorted(calendar)
    return [d for d, nxt in zip(days, days[1:])
            if d >= min(sent) and d not in sent and now >= datetime.combine(nxt, SEND_DEADLINE, ET)]


def replay(acct: AccountStore, journal: Journal, store: Store) -> dict:
    """Rebuild the history as prepare_plan did, from the strategy copy saved with the journal. If the input hash
    matches, decide again; targets that differ mean the strategy depends on more than history and as_of."""
    plan = plan_from_json(journal.plan())
    path = journal.folder / "strategy.py"
    strategy = _load_exactly(path.read_bytes(), str(path))
    prices = prepare(load_bars(store, decision_time(plan.day), universe()), plan.dividends)
    stamp = pd.Timestamp(plan.day)
    history = history_as_of(prices, stamp)
    if input_hash(history) != plan.input_hash:
        return {"hash_matches": False, "same_targets": None, "targets": None}
    try:
        targets = decide(strategy, history, stamp, set(prices.raw["c"].columns))
    except StrategyError:
        return {"hash_matches": True, "same_targets": False, "targets": None}
    return {"hash_matches": True, "same_targets": targets == plan.targets, "targets": targets}


def export_fills(fills: pd.DataFrame, path: str) -> None:
    tmp = f"{path}.tmp"
    fills.to_csv(tmp, index=False)
    os.replace(tmp, path)
