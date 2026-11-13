"""The paper-trading daily step: the Alpaca broker wrapper and preparing a plan. No Qt imports."""
import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import CorporateActionsType
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.requests import CorporateActionsRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetCalendarRequest, GetOrdersRequest, GetPortfolioHistoryRequest,
                                     MarketOrderRequest)

from app.backtest import StrategyError, decide, history_as_of, input_hash, load_strategy, prepare
from app.daily import ET, load_bars, refresh_daily, universe
from app.paper_plan import Limits, LossCheck, Note, Order, check_losses, plan_orders, run_window
from app.paper_state import AccountStore
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
    last_bars: dict[str, dict]  # day-T bar per held or targeted symbol


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


def _remainder(acct: AccountStore, day: date, attempts: list[int]) -> Plan | None:
    """The latest attempt's saved plan, cut to its unsent orders, if that attempt is still open."""
    if not attempts:
        return None
    j = acct.journal(day, attempts[-1])
    try:
        saved = plan_from_json(j.plan())
    except FileNotFoundError:  # a crash before plan.json was written
        return None
    sent = j.order_states()
    unsent = [o for o in saved.orders if o.client_order_id not in sent]
    return replace(saved, orders=unsent) if unsent and not j.closed() else None


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

        prefix = f"sb-{account.number}-{day:%Y%m%d}-a"
        ours = re.compile(rf"{re.escape(prefix)}(\d+)-")
        remote = {int(m[1]) for o in broker.orders_since(datetime.combine(day, time(), ET))
                  if (m := ours.match(o.client_order_id or ""))}
        local = acct.attempts(day)
        left = _remainder(acct, day, local)
        mine = f"{prefix}{left.attempt}-" if left else None
        blocking = [o.client_order_id for o in broker.open_orders()
                    if not (mine and (o.client_order_id or "").startswith(mine))]
        if blocking:
            return Refusal(f"{len(blocking)} open order(s) from another day or attempt, such as {blocking[0]}. "
                           "Wait for them to finish or cancel them.")
        if left:
            return left  # never recomputed: same IDs and quantities as confirmed
        attempt = max([*local, *remote], default=0) + 1
        notes = []
        if remote - set(local):
            notes.append(Note(None, "warning", f"Alpaca has orders for {day} that this computer has no journal "
                                               "for: another computer may be running this account."))

        path = settings["strategy_path"]
        if not path:
            return Refusal("No strategy file is chosen.")
        if not Path(path).is_file():
            return Refusal(f"Strategy file not found: {path}")
        code = Path(path).read_bytes()
        try:
            strategy = load_strategy(path)
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
        equity = broker.equity_history()
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
                    last_bars)

        # step 8: journal it before anyone can confirm it
        journal = acct.journal(day, attempt)
        (journal.folder / "strategy.py").write_bytes(code)
        journal.write_plan(plan_to_json(plan))
        return plan
