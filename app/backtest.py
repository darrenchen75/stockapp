"""Backtest price view: on decision day T a strategy sees earlier prices restated only for events up to T."""
from dataclasses import dataclass

import pandas as pd


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
