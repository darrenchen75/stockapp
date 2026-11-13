"""Pure rules for a paper-trading run: the run window, order IDs, sizing and the loss check. No I/O."""
import math
from dataclasses import dataclass
from datetime import date, datetime, time

from app.daily import ET, decision_time

SEND_DEADLINE = time(9, 28)  # New York time on T+1
MAX_ORDER_ID = 48


@dataclass(frozen=True)
class Limits:
    max_weight: float = 0.25
    loss_cutoff: float = 0.03
    cash_buffer: float = 0.02


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str  # "buy" | "sell"
    qty: int
    client_order_id: str
    ref_price: float  # T close, or nan


@dataclass(frozen=True)
class Note:
    symbol: str | None
    kind: str  # trim_cap | blocked_loss | blocked_split | no_close | fractional
    text: str


@dataclass(frozen=True)
class LossCheck:
    breaches: list[tuple[date, float]]
    checked_through: date | None
    problem: str | None


def run_window(calendar: list[date], now: datetime) -> tuple[date, date, str | None]:
    """(T, T+1, reason the window is closed or None). T is the latest session whose 20:30 has passed."""
    done = [d for d in calendar if decision_time(d) <= now]
    if not done:
        today = now.astimezone(ET).date()
        return today, today, "No session has reached 20:30 New York time yet."
    t = max(done)
    later = [d for d in calendar if d > t]
    if not later:
        return t, t, f"The calendar has no session after {t}."
    nxt = min(later)
    if now >= datetime.combine(nxt, SEND_DEADLINE, ET):
        return t, nxt, f"The 09:28 New York send deadline for {nxt} has passed."
    return t, nxt, None


def order_id(account_number: str, day: date, attempt: int, symbol: str) -> str:
    out = f"sb-{account_number}-{day:%Y%m%d}-a{attempt}-{symbol}"
    if len(out) > MAX_ORDER_ID:
        raise ValueError(f"order id {out!r} is over {MAX_ORDER_ID} characters")
    return out


def plan_orders(targets: dict[str, float], positions: dict[str, float], equity: float, closes: dict[str, float],
                limits: Limits, loss_locked: bool, split_symbols: set[str],
                account_number: str, day: date, attempt: int) -> tuple[list[Order], list[Note]]:
    sells, buys, notes = [], [], []
    for s in sorted(set(targets) | {s for s, q in positions.items() if q}):
        held = positions.get(s, 0.0)
        whole = math.floor(held)
        if s in split_symbols:
            notes.append(Note(s, "blocked_split", f"{s}: a split is pending, so no order"))
            continue
        if held != whole:
            notes.append(Note(s, "fractional", f"{s}: {held - whole:.4f} of a share is left alone"))
        close = closes.get(s)
        if close is None or not close > 0:  # also catches nan
            if s in targets:
                notes.append(Note(s, "no_close", f"{s}: no close for the day, so no order"))
            elif whole > 0:
                notes.append(Note(s, "no_close", f"{s}: no close for the day; selling to zero"))
                sells.append(Order(s, "sell", whole, order_id(account_number, day, attempt, s), math.nan))
            continue
        want = math.floor(targets[s] * equity * (1 - limits.cash_buffer) / close) if s in targets else 0
        if want < whole:  # sales are never trimmed
            sells.append(Order(s, "sell", whole - want, order_id(account_number, day, attempt, s), close))
            continue
        cap = math.floor(limits.max_weight * equity / close)
        qty = max(0, min(want, cap) - whole)
        if want - whole > qty:
            notes.append(Note(s, "trim_cap", f"{s}: buy cut to {qty} by the {limits.max_weight:.0%} position cap"))
        if qty and loss_locked:
            notes.append(Note(s, "blocked_loss", f"{s}: buy of {qty} dropped by the daily loss lock"))
        elif qty:
            buys.append(Order(s, "buy", qty, order_id(account_number, day, attempt, s), close))
    return sells + buys, notes


def check_losses(history: list[tuple[date, float]], after: date | None, cutoff: float) -> LossCheck:
    """history is (session date, closing equity). Each session's loss is equity / previous equity - 1."""
    if not history:
        return LossCheck([], None, "No portfolio history to check the loss cutoff against.")
    h = sorted(history)
    through = h[-1][0]
    if after is None:
        start = max(len(h) - 1, 1) if len(h) > 1 else len(h)  # only the latest session; a lone one has no previous
    else:
        dates = [d for d, _ in h]
        if after not in dates:
            return LossCheck([], through, f"{after} is not in the portfolio history.")
        start = dates.index(after) + 1
    breaches = []
    for i in range(start, len(h)):
        day, eq = h[i]
        prev = h[i - 1][1]
        if eq == 0 or (prev == 0 and after is not None):
            return LossCheck(breaches, through, f"Equity of 0 in the history for {day if eq == 0 else h[i - 1][0]}.")
        if prev == 0:  # first run: the funding day
            continue
        loss = eq / prev - 1
        if loss < -cutoff:
            breaches.append((day, loss))
    return LossCheck(breaches, through, None)
