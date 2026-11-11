# Paper Trading Design

Date: 2026-11-11. Status: draft for owner review. Builder check done; its questions are
answered below.

Builds on `docs/superpowers/specs/2026-11-06-strategy-backtest-design.md` (the module
spec: strategy interface, decision time, data) and
`docs/superpowers/specs/2026-11-08-backtest-compare-screen-design.md` (the compare
screen). Covers the reviewer's feedback items 3 (paper by default), 4 (decision and
order log), 5 (safety limits) and 6 (fill tracking), and finishes item 9 by showing the
paper account next to its backtests.

## Intent

The reviewer wants the app to be "the home for a strategy". The backtest shows what a
strategy would have done. Paper trading shows what it does with real market timing and
real fills, using fake money. Its job is to show whether live results track the
backtest, and to trace why when they don't, before real money is ever considered.

The module spec's rule was "no orders until reconciliation (item 8) passes". The owner
lifted that rule on 2026-11-11: paper orders are built now, accepting that the
reviewer's test case may change timing, fill or cost assumptions and force rework.

## Who uses it

The owner and the reviewer each run their own copy, on their own computers, with their
own Alpaca paper account and their own strategy file. One strategy owns one whole paper
account (owner's decision): the account's positions are the strategy's positions.

## Goals

1. Run the same `target_weights` strategy file the backtest uses, with no second
   version of its logic.
2. Once a day, turn its targets into at-the-open orders on the user's Alpaca paper
   account, after a person has seen and confirmed them.
3. Enforce a maximum position size, a daily loss cutoff and a kill switch.
4. Log every run: what the strategy saw, what it returned, what was sent, what was
   blocked or trimmed and why.
5. Record each fill against the price the backtest would have assumed.
6. Show the paper account's performance in the compare screen beside backtests.

## Non-goals

- Live (real money) trading. The switch exists in the design but cannot be turned on
  in this stage (see "Paper by default").
- Running without the app open: schedulers, background jobs, servers. The owner chose a
  button "for now" and said that may change, so the daily step is plain code the UI
  calls (see "The daily step").
- Several strategies in one account.
- Item 8 (reconciliation): waits on the reviewer's test case.
- Item 10 (executive oversight): not yet defined by the reviewer. The confirm step and
  the kill switch are the only oversight in this stage.
- Intraday trading, shorts, leverage (the strategy interface stays long or flat, total
  weight at most 1).
- Syncing logs between computers. Logs stay on each computer, like backtest runs.

## Anti-goals

This has failed, even if it works, if any of these is true:

- The strategy's code path differs between backtest and paper.
- Any order reaches Alpaca without a person confirming that run's order list.
- Any path in this stage can reach Alpaca's live trading endpoint.
- One day's decision can send the same order twice (a double click, a retry, a restart).
- The strategy sees a bar from before that day's 20:30 New York cutoff has passed.
- A run's log can't be traced to the exact strategy code, settings and input data.
- Existing viewer, backtest or compare behavior changes, except the added paper line.

## The daily step

Decision time stays as in the module spec: 20:30 New York time on day T. Alpaca accepts
at-the-open orders (`time_in_force=opg`) from about 19:00 until 09:28 New York time; an
unfilled at-the-open order is cancelled by Alpaca after the open.

T+1 means the next trading day, so a Friday's run can happen over the weekend if Alpaca
accepts at-the-open orders then; the builder checks. A run is allowed from 20:30 on day T
until 09:28 on T+1 (New York time). Outside
that window the button is disabled with the reason shown. A run for day T after 09:28
on T+1 is refused; that day is simply missed.

When the user clicks **Run today**:

1. Refresh daily data through day T using the existing daily refresh. If day T's data
   is not final or is incomplete (the same checks the backtest uses), stop with a clear
   message. No orders.
2. Build the strategy's input exactly as the backtest runner does for day T (same
   function, same adjustment rule) and call `target_weights(history, as_of=T)`. Validate
   the result with the runner's existing validation.
3. Read the account from Alpaca: equity, previous close equity (`last_equity`), cash,
   positions, and open orders.
4. Work out orders: target value per symbol = weight × equity × (1 − cash buffer); target shares =
   floor(target value / day T's close); order = target shares − current shares. Symbols
   held but not targeted go to zero.
5. Apply safety limits (below), recording every trim and block.
6. Show the planned orders: symbol, side, shares, approximate value, current and target
   weight, and any trims, blocks or warnings. Nothing is sent yet.
7. On **Send**, submit each order as a market order with `time_in_force=opg`, sales
   first. Each order's `client_order_id` is built from the strategy's fingerprint, day T,
   an attempt number and the symbol, so a repeated submission of the same order is
   rejected by Alpaca as a duplicate rather than doubling it (see "Interrupted sends").
8. Write the log entry (below), whether the user sent, cancelled, or the run failed.

If open orders from any other day or attempt exist when a run starts, the run stops and
asks the user to wait or cancel them, so two days' orders never stack.

### Interrupted sends (designer's call)

- **Send fails partway** (network, crash): the log keeps the planned list and which
  orders Alpaca confirmed. Running again for the same day and attempt shows only the
  unsent remainder and offers to send it. Open orders from that same attempt don't stop
  the run. The IDs of already-sent orders are reused, so nothing is sent twice.
- **Kill switch, then released the same night:** the cancelled attempt is closed. A new
  run for the same day starts a new attempt (new IDs), recomputes from the account as it
  now stands, and goes through confirm again.
- **Split taking effect at T+1's open** for a symbol to be bought or sold: that symbol's
  order is blocked for the day with a warning, since day T's close no longer sizes it
  correctly.

### Cash (owner's decision)

All of a day's at-the-open orders fill in the same opening auction, so buys can't wait
for that morning's sales, and a gap up makes buys cost more than planned. The backtest
never borrows. To match it:

- Targets are sized from equity minus a cash buffer (default 2%, editable, logged).
- The app never sizes a buy beyond what the targets and buffer allow. A buy that still
  doesn't fit the cash projected after that day's sales is trimmed and logged.
- **Account margin:** the owner's choice is margin off on the paper account, so it can
  never borrow. The builder checks first whether Alpaca, with margin off, rejects buys
  that rely on same-auction sales. If it does, margin stays on, the buffer alone keeps
  cash at or above zero, and any day that ends with negative cash is flagged in the log
  and the paper window. The owner sees this result before the build goes past it.

The steps above live in a module with no Qt imports, taking clients and the store as
arguments, so a scheduler or server can call it later without a rewrite. The window
only calls it and shows the result. Network calls run off the UI thread, as the compare
screen already does.

### Whole shares (designer's call)

Orders are whole shares sized from day T's close, because the next open is unknown when
the order is sent. The backtest uses fractional shares at T+1's open. The difference is
small for a large account and shows up in fill tracking. The builder must check whether
Alpaca accepts fractional at-the-open orders; if it does, the owner decides whether to
use them.

## Safety limits

Set when paper trading is first set up for an account, stored with each log entry, and
editable later (an edit is itself logged).

- **Maximum position size:** a percent of account equity per symbol (default 25%).
  A buy that would take a symbol over the cap, valued at day T's close, is trimmed to
  the cap. The trim is shown before sending and logged. Sales are never trimmed.
- **Daily loss cutoff:** a percent (default 3%). It compares day T's closing equity
  with day T−1's, both from Alpaca's portfolio history, so it measures the same day
  whether the run happens at 21:00 or 08:00 (designer's call: Alpaca's `last_equity`
  moves overnight). If the loss is larger, the run sends no buys and a red banner
  appears. **The banner is a lock (owner's decision):** no run on any later day sends
  buys until a person clears it; clearing is logged. Sales toward the targets still go
  out. Nothing is sold just because the cutoff was hit.
- **Kill switch:** one button, always enabled when an account is set up. Pressing it
  cancels every open order in the account immediately and halts: no further runs until
  a person turns the halt off (logged). Positions are left alone. A separate **Sell
  everything** button closes all positions (at market if the market is open, otherwise
  at the next open) after its own confirmation.

The halt state and cleared-banner state are stored on disk, so restarting the app does
not undo them.

## Paper by default

- The trading client is always created with Alpaca's paper flag in this stage. The live
  option appears in the paper window as a disabled switch with the reason: the app has
  no code signing, so a tampered update could trade real money. Turning it on is a later
  design, with separate live keys.
- The app uses the Alpaca keys already stored in the keyring (paper keys work for both
  data and paper trading). Keys never go in the repo, the logs or exported files.
- At setup, the app checks the account is a paper account (by endpoint) and shows the
  account number so the user can confirm it is the right one.

## The log

One entry per run, stored on this computer next to the backtest runs, in a format the
builder picks that can be opened outside the app (for example a folder per day with
CSV and JSON files). Each entry records:

- Run time, day T, app version, the user's choice (sent, cancelled, failed) and any
  error text.
- The strategy file's path, a copy of its code, and its SHA-256 (saved runs keep only
  the copy today; the hash is new).
- The dividends setting (a paper setting, on by default, matching the backtest's).
- **What the strategy saw:** a fingerprint (hash) of the exact input passed to it,
  with prices rounded to a fixed precision before hashing so tiny factor changes after
  later corporate actions don't break it, the
  data date, and the last day's bars for every symbol it held or targeted. The full
  input is not copied (years of bars for about 500 symbols).
- Its returned targets.
- The account snapshot it worked from: equity, last equity, cash, positions.
- Every planned order, every trim and block with its reason, and every sent order with
  its `client_order_id` and Alpaca order ID.
- Safety settings in force.

**Replay:** a button on a log entry rebuilds the input from the stored daily data,
checks the fingerprint matches, and re-runs the saved strategy copy to show whether it
returns the same targets. If the fingerprint doesn't match (the daily data was since
re-downloaded or re-adjusted), it says so rather than replaying on different data.

## Fill tracking

After the open on T+1, the next time the app is open it fetches the fills for orders
it sent (by `client_order_id`) and records, per order:

- Shares ordered and shares filled (an unfilled or partly filled at-the-open order is
  recorded as such).
- Average fill price.
- **Expected price:** T+1's official open from the daily bars, which is what the
  backtest assumes. Difference in basis points.
- Day T's close (the sizing price). Difference in basis points.

The paper window shows these per order and as an average over all fills, so real
trading costs can be compared with a backtest's `cost_bps`. Exportable as CSV.

## Performance in the compare screen

The paper account appears in the compare screen's run list as pinned entries (like
SPY), one per strategy (owner's decision): "Paper: <strategy name>". Each line starts at
the first day orders were sent for that strategy file and ends when the account moved to
a different strategy (or today). Account history before the app's first sent run is
ignored. Daily values come from Alpaca's portfolio history
(daily equity), not from the app's own snapshots, because the app is not open every
day. Deposits and withdrawals don't happen on a paper account; if the builder finds the
history includes any, they are excluded from returns.

It gets the same rows as a run: total return, yearly growth, worst drop, volatility,
beta and alpha against SPY, each with its day count. Ticking it next to a backtest of
the same strategy over the same dates shows live-versus-backtest drift directly. The
"best" marking ignores it, as it ignores SPY.

## The paper window

Opened from the main window's Tools menu ("Paper trading…"), like Backtests. It shows:

- The account (number, paper badge, equity), the strategy file, safety settings, and the
  disabled live switch.
- The last run's day and result, and a warning if one or more trading days were missed.
- **Run today** (enabled only in the run window), the planned-orders table, and
  **Send** / **Cancel**.
- The red loss-cutoff banner when active, with **Clear**.
- **Kill switch** and **Sell everything**.
- The log list (newest first), each opening its detail, fills and **Replay**.

Layout is left to the builder, following the compare screen's style.

## Calls made by the designer (owner to check)

- Interrupted sends resume the same attempt; a kill switch release starts a new attempt.
- The daily loss cutoff measures day T's close against day T−1's from portfolio history.
- A symbol with a split at T+1's open is blocked for that day.
- Weekend and holiday runs target the next trading day.
- Orders are worked out from Alpaca's real positions, not a ledger the app keeps. If
  someone trades by hand in the account, the next run corrects it toward the targets.
- Whole shares sized from day T's close (see "Whole shares").
- Order IDs built from strategy fingerprint, day, attempt and symbol, to prevent
  duplicates.
- A run is refused outside 20:30 on T to 09:28 on T+1, and stops if open orders exist.
- Daily data is refreshed as the run's first step.
- Paper account performance comes from Alpaca's portfolio history.
- Default limits: 25% per symbol, 3% daily loss, 2% cash buffer. Editable.
- Kill switch leaves positions alone; selling is a separate confirmed button.
- Logs stay on each computer and are never synced.

## First build step

Several statements above about Alpaca come from memory, not from tests. Before anything
else, the builder checks them against a real paper account and reports to the owner:
fractional at-the-open orders, weekend submission, `client_order_id` reuse after a
cancel, buying power for buys alongside same-auction sales with margin off, and what
portfolio history returns for a new account.

## Left to the builder

Module and function layout, log file format, how state (halt, banner, settings,
strategy path) is stored, the window layout, the exact wording of messages, and how
progress is shown, within the constraints above.
