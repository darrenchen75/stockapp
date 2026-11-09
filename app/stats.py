"""Numbers and chart series for saved backtest results (pandas only)."""
import pandas as pd

BASE = 100_000.0


def growth(portfolio: pd.DataFrame) -> pd.Series:
    v = portfolio.set_index("date")["value"]
    return v / v.iloc[0] * BASE


def drawdown(portfolio: pd.DataFrame) -> pd.Series:
    v = portfolio.set_index("date")["value"]
    return v / v.cummax() - 1


def summary(portfolio: pd.DataFrame, positions: pd.DataFrame) -> dict[str, float | None]:
    v = portfolio["value"]
    days = (portfolio["date"].iloc[-1] - portfolio["date"].iloc[0]).days
    ratio = v.iloc[-1] / v.iloc[0]
    filled = positions[positions["fill_price"].notna()]
    return {"total_return": float(ratio - 1),
            "cagr": float(ratio ** (365.25 / days) - 1) if days else None,
            "worst_drop": float(drawdown(portfolio).min()),
            "rebalances": int(filled["decided_on"].nunique()),
            "warnings": int(positions["warning"].fillna("").ne("").sum())}


def best(column_values: dict[str, dict[str, float | None]]) -> dict[str, str]:
    out = {}
    for key in ("total_return", "cagr", "worst_drop"):  # worst_drop is <= 0, so highest = closest to zero
        scored = {run: s[key] for run, s in column_values.items() if s[key] is not None}
        if scored:
            out[key] = max(scored, key=scored.get)
    return out
