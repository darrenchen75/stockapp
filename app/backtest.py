"""Backtest price view: on decision day T a strategy sees earlier prices restated only for events up to T."""
import argparse
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.corporate_actions import CorporateActionsClient

from app.daily import default_db_path, load_bars, refresh_daily, universe
from app.store import Store


@dataclass(frozen=True)
class Prices:
    raw: dict[str, pd.DataFrame]  # o, h, l, c, v: dates by symbols, NaN where no bar
    factor: pd.DataFrame  # price adjustment factor, forward-filled per symbol
    split: pd.DataFrame  # split-only factor, forward-filled per symbol
    days: list[pd.Timestamp]


def prepare(bars: pd.DataFrame, dividends: bool = True) -> Prices:
    bars = bars.assign(date=pd.to_datetime(bars["date"]))
    for col in ("factor_all", "factor_split"):
        if col not in bars:
            bars[col] = 1.0
    wide = lambda col: bars.pivot(index="date", columns="symbol", values=col).sort_index()
    raw = {col: wide(col) for col in ("o", "h", "l", "c", "v")}
    spy = bars.loc[bars["symbol"] == "SPY", "date"]
    days = sorted(spy.unique() if len(spy) else bars["date"].unique())
    return Prices(raw, wide("factor_all" if dividends else "factor_split").ffill(),
                  wide("factor_split").ffill(), [pd.Timestamp(d) for d in days])


def history_as_of(prices: Prices, day: pd.Timestamp) -> dict[str, pd.DataFrame]:
    factor = prices.factor.loc[:day]
    split = prices.split.loc[:day]
    out = {col: prices.raw[col].loc[:day] * factor / factor.iloc[-1] for col in ("o", "h", "l", "c")}
    out["v"] = prices.raw["v"].loc[:day] * split.iloc[-1] / split
    return out


class StrategyError(Exception):
    """A strategy raised or returned invalid weights; the message names the decision date."""


def validate_weights(weights, symbols, day) -> dict[str, float]:
    def fail(why):
        return StrategyError(f"{pd.Timestamp(day):%Y-%m-%d}: {why}")

    if not isinstance(weights, dict):
        raise fail(f"expected a dict of weights, got {type(weights).__name__}")
    out = {}
    for sym, w in weights.items():
        if sym not in symbols:
            raise fail(f"unknown symbol {sym!r}")
        try:
            w = float(w)
        except (TypeError, ValueError):
            raise fail(f"weight for {sym} is not a number") from None
        if not 0 <= w <= 1:  # also rejects NaN
            raise fail(f"weight for {sym} is {w}, outside 0 to 1")
        out[sym] = w
    if sum(out.values()) > 1 + 1e-9:
        raise fail(f"weights total {sum(out.values())}, above 1")
    return out


@dataclass(frozen=True)
class Fill:
    shares: dict[str, float]
    cash: float
    scaled: bool  # purchases were cut back to fit the cash
    unfilled: list[str]  # targets with no bar at this open


def rebalance(shares, cash, targets, open_px, last_px, cost_bps=0.0) -> Fill:
    """Sell first, then buy from the cash; scale purchases by one factor if they cost more than the cash."""
    cost = cost_bps / 10_000
    price = lambda s: open_px[s] if s in open_px else last_px[s]
    value = cash + sum(n * price(s) for s, n in shares.items())
    tol = 1e-9 * value
    new, buys = dict(shares), {}
    for s in sorted(open_px.keys() & (shares.keys() | targets.keys())):
        delta = targets.get(s, 0) * value / open_px[s] - shares.get(s, 0)
        if abs(delta) * open_px[s] <= tol:
            continue
        if delta < 0:
            cash += -delta * open_px[s] * (1 - cost)
            new[s] += delta
        else:
            buys[s] = delta
    need = sum(d * open_px[s] for s, d in buys.items()) * (1 + cost)
    scale = cash / need if need > cash else 1.0
    for s, d in buys.items():
        new[s] = new.get(s, 0) + d * scale
    left = cash - need * scale
    if left < -tol:  # only rounding error may be clamped away
        raise ValueError(f"rebalance would leave negative cash: {left}")
    return Fill({s: n for s, n in new.items() if n > 0}, max(0.0, left), need - cash > tol,
                sorted(s for s, w in targets.items() if w > 0 and s not in open_px))


@dataclass(frozen=True)
class Result:
    portfolio: pd.DataFrame  # date, value, return, cash, warning
    positions: pd.DataFrame  # decided_on, date, symbol, target_weight, fill_price, shares, warning
    dividends: bool


def run(strategy, bars, start, end, capital=100_000.0, cost_bps=0.0, dividends=True) -> Result:
    """Decide on each day's final bar, fill at the next open, value at that day's close."""
    prices = prepare(bars, dividends)
    days = [d for d in prices.days if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    if not days:
        raise ValueError(f"no trading days between {start} and {end}")
    symbols = set(prices.raw["c"].columns)
    # adjusted units: value is shares * raw * factor, so splits and dividends never change it
    adj_open = prices.raw["o"] * prices.factor
    adj_close = (prices.raw["c"] * prices.factor).ffill()
    shares, cash, pending, port, pos = {}, float(capital), None, [], []

    def held(day):  # shares as they would count on this day
        return {s: n * prices.factor.at[day, s] for s, n in shares.items()}

    for i, day in enumerate(days):
        note = None
        if pending:
            decided, targets = pending
            open_px = adj_open.loc[day].dropna().to_dict()
            fill = rebalance(shares, cash, targets, open_px, adj_close.loc[days[i - 1]].dropna().to_dict(), cost_bps)
            after = fill.shares
            for s in sorted(set(targets) | set(shares) | set(after)):
                if s in open_px:
                    why = None
                elif s in shares:
                    why = "no bar today: position kept at its last price"
                else:
                    why = "no bar today: target not filled" if s in fill.unfilled else None
                traded = after.get(s, 0) != shares.get(s, 0)
                pos.append(dict(decided_on=decided, date=day, symbol=s, target_weight=targets.get(s, 0.0),
                                fill_price=prices.raw["o"].at[day, s] if traded else float("nan"),
                                shares=after[s] * prices.factor.at[day, s] if s in after else 0.0, warning=why))
            shares, cash = after, fill.cash
            note = "purchases scaled to the available cash" if fill.scaled else None
        port.append(dict(date=day, cash=cash, warning=note,
                         value=cash + sum(n * adj_close.at[day, s] for s, n in shares.items())))
        try:
            weights = strategy(history_as_of(prices, day), day)
        except Exception as e:
            raise StrategyError(f"{day:%Y-%m-%d}: strategy raised {type(e).__name__}: {e}") from e
        pending = day, validate_weights(weights, symbols, day)
    decided, targets = pending
    now = held(days[-1])
    for s in sorted(set(targets) | set(now)):  # the last decision has no fill
        pos.append(dict(decided_on=decided, date=pd.NaT, symbol=s, target_weight=targets.get(s, 0.0),
                        fill_price=float("nan"), shares=now.get(s, 0.0), warning=None))
    portfolio = pd.DataFrame(port, columns=["date", "value", "return", "cash", "warning"])
    portfolio["return"] = portfolio["value"].pct_change().fillna(0.0)
    cols = ["decided_on", "date", "symbol", "target_weight", "fill_price", "shares", "warning"]
    return Result(portfolio, pd.DataFrame(pos, columns=cols), dividends)


def load_strategy(path: str):
    try:
        spec = importlib.util.spec_from_file_location("strategy", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        raise StrategyError(f"cannot load {path}: {type(e).__name__}: {e}") from e
    if not callable(getattr(module, "target_weights", None)):
        raise StrategyError(f"{path} must define target_weights(history, as_of)")
    return module.target_weights


def write_results(result: Result, out_dir: str, source: str) -> None:
    note = (f"# Today's S&P 500 members only, so results have survivorship bias. "
            f"Data: {source}. Dividends: {'on' if result.dividends else 'off'}.\n")
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for name, frame in (("portfolio.csv", result.portfolio), ("positions.csv", result.positions)):
        with open(Path(out_dir) / name, "w", newline="") as f:
            f.write(note)
            frame.to_csv(f, index=False)


SOURCE = "Alpaca SIP daily; Alpaca dividends miss some payouts (e.g. SPY 2016-03-18, 2018-06-15)"


def main(argv=None) -> int:
    from app import settings  # imported here because it pulls in Qt

    ap = argparse.ArgumentParser(prog="python -m app.backtest", description="Backtest a strategy file.")
    ap.add_argument("strategy")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--out", required=True, help="directory for portfolio.csv and positions.csv")
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--cost-bps", type=float, default=0.0)
    ap.add_argument("--dividends", action=argparse.BooleanOptionalAction, default=True)
    args = ap.parse_args(argv)

    def fail(message):
        print(message, file=sys.stderr)
        return 1

    keys = settings.load_keys()
    if keys is None:
        return fail("No Alpaca key found. Open Stock Bars once and enter your Alpaca key.")
    try:
        strategy = load_strategy(args.strategy)
        store = Store(default_db_path())
        try:
            refresh_daily(StockHistoricalDataClient(*keys), CorporateActionsClient(*keys), store, universe(),
                          progress=lambda done, total: print(f"downloaded {done}/{total}", file=sys.stderr))
        except Exception as e:
            return fail(f"Could not download daily data: {e}")
        result = run(strategy, load_bars(store), args.start, args.end, args.capital, args.cost_bps, args.dividends)
        write_results(result, args.out, SOURCE)
    except (StrategyError, ValueError) as e:
        return fail(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
