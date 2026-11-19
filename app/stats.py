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
            "warnings": int(positions["warning"].dropna().astype(str).str.strip().ne("").sum())}


def best(column_values: dict[str, dict[str, float | None]]) -> dict[str, str]:
    out = {}
    for key in ("total_return", "cagr", "worst_drop"):  # worst_drop is <= 0, so highest = closest to zero
        scored = {run: s[key] for run, s in column_values.items() if s[key] is not None}
        if scored:
            out[key] = max(scored, key=scored.get)
    return out


def risk(portfolio: pd.DataFrame, benchmark: pd.DataFrame | None) -> dict[str, float | int | None]:
    """Yearly volatility, and beta and yearly alpha against `benchmark` (risk-free 0) with their standard errors.
    `days` is the count of daily returns each figure rests on: the run's own for volatility, the shared dates
    for beta and alpha."""
    r = portfolio.set_index("date")["value"].pct_change().dropna()
    out = {"volatility": float(r.std() * 252 ** .5) if len(r) > 1 else None, "vol_days": len(r),
           "beta": None, "beta_se": None, "alpha": None, "alpha_se": None, "days": len(r)}
    if benchmark is None:
        return out
    both = pd.concat([r, benchmark.set_index("date")["value"].pct_change()], axis=1, join="inner").dropna()
    y, x = both.iloc[:, 0], both.iloc[:, 1]
    n, sxx = len(both), ((x - x.mean()) ** 2).sum()
    out["days"] = n
    if n < 3 or sxx == 0:  # the errors need n - 2 > 0 and a benchmark that moved
        return out
    beta = ((x - x.mean()) * (y - y.mean())).sum() / sxx
    alpha = y.mean() - beta * x.mean()
    s2 = ((y - alpha - beta * x) ** 2).sum() / (n - 2)
    out.update(beta=float(beta), beta_se=float((s2 / sxx) ** .5), alpha=float(alpha * 252),
               alpha_se=float((s2 * (1 / n + x.mean() ** 2 / sxx)) ** .5 * 252))
    return out
