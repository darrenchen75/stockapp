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
unfilled at-the-open order is cancelled by Alpaca after the open. An at-the-open order
sent after 19:00 joins the *next* opening auction, so a stale plan sent late would trade
on the wrong day; the send-time checks below prevent that.

**Day T and T+1** come from Alpaca's market calendar, not from the daily data: T is the
latest trading session whose 20:30 cutoff has passed, and T+1 is the next session. A
run is allowed from 20:30 on T until 09:28 on T+1, New York time (weekends and holidays
included, if Alpaca accepts at-the-open orders then; the builder checks). Outside that
window the button is disabled with the reason shown. After 09:28 on T+1, day T is missed.

When the user clicks **Run today**:

1. Take the account lock (see "One writer per account"). Read the account identity
   (account ID and endpoint). Stop if the account is halted.
2. Refresh daily data through day T using the existing daily refresh, then verify
   coverage directly: SPY's last stored bar must be day T, and every symbol held or in
   the strategy's universe must either have a day T bar or be listed in a warning. The
   refresh's "done through" marker must record verified coverage, not just the date it
   asked for; the existing marker logic needs that change. If SPY has no day T bar, stop.
   No orders.
3. Build the strategy's input exactly as the backtest runner does for day T (same
   function, same adjustment rule) and call `target_weights(history, as_of=T)`. Validate
   the result with the runner's existing validation.
4. Read the account from Alpaca: equity, cash, buying power, positions, open orders and
   portfolio history.
5. Fetch corporate actions for every held or targeted symbol through T+1 (a separate
   query from the daily refresh, which stops at T). A symbol with a split taking effect
   at T+1's open is blocked for the day with a warning. If this query fails, the run
   stops: missing split information never counts as "no split".
6. Work out orders: target value per symbol = weight × equity × (1 − cash buffer);
   target shares = floor(target value / day T's close); order = target shares − current
   shares. Symbols held but not targeted go to zero.
7. Apply safety limits (below), recording every trim and block.
8. Save the plan to the journal (below) as a fixed list: account ID, day T, attempt,
   every order with its `client_order_id`, and the account state it was built from.
9. Show the planned orders: symbol, side, shares, approximate value, current and target
   weight, and any trims, blocks or warnings. Nothing is sent yet.
10. On **Send**, recheck before anything goes out: still before 09:28 on T+1, account
    and endpoint unchanged, not halted, loss lock unchanged, safety settings unchanged,
    no open orders except this attempt's, and positions still equal to those the plan
    was built from. Any difference discards the plan and asks for a new run and a new
    confirmation.
11. Submit the saved orders exactly as confirmed, as market orders with
    `time_in_force=opg`, sales first. Before each request, journal "sending <id>"; after
    Alpaca answers, journal the result. Check the halt flag before each order.
12. Write the log entry (below), whether the user sent, cancelled, or the run failed.

`client_order_id` is built from the account ID, day T, attempt number and symbol, not
the strategy, so any two plans for the same account, day and attempt collide at Alpaca
instead of both going through. If open orders from any other day or attempt exist when
a run starts, the run stops and asks the user to wait or cancel them, so two days'
orders never stack.

The steps above live in a module with no Qt imports, taking clients and the store as
arguments, so a scheduler or server can call it later without a rewrite. The window
only calls it and shows the result. Network calls run off the UI thread, as the compare
screen already does.

### One writer per account

The owner uses several computers, and two windows could be open on one. Only one plan
per account and day may be live:

- On one computer, a lock file per account stops a second window from running or
  sending.
- Across computers, nothing local can see the other machine, so the broker is the
  shared record: before allocating an attempt number, the run lists the account's
  orders for day T (by the `client_order_id` prefix) and takes the next unused attempt.
  If any order from day T is still open, the run stops. Two computers that both pick
  attempt 1 produce the same IDs, so Alpaca accepts only the first. The rule for users:
  run each account from one computer at a time; the app warns when it finds day T
  orders it has no journal for.

### The journal and recovery

The journal is written to disk before any order is sent, and updated before and after
each request, so a crash at any point can be recovered:

- On the next start, any order journaled as "sending" with no recorded result is
  looked up at Alpaca by its `client_order_id`. Its real state (accepted, filled,
  rejected, not found) is recorded before anything else can be sent.
- **Send fails partway:** running again for the same day shows the confirmed plan's
  unsent remainder, with the same IDs and quantities as confirmed, never recomputed,
  and offers to send it after the send-time checks pass.
- **Kill switch:** the halt is written to disk first, then every open order is
  cancelled, and the sender checks the halt before each order, so nothing new goes out
  after it. The attempt stays open until Alpaca reports every one of its orders as
  cancelled, filled or rejected; any it can't confirm are listed in red. Only then can
  the halt be released, and a new run for the same day starts a new attempt (new IDs),
  recomputed from the account as it now stands, through confirm again.
- **Sell everything** is a journaled operation like a daily run: it sets the halt,
  cancels and confirms every open order is finished (so a queued buy can't refill the
  account afterwards), shows the list of sales for confirmation, sends them with their
  own IDs, and tracks each to a finished state. A restart resumes it the same way.

### Cash (owner's decision)

All of a day's at-the-open orders fill in the same opening auction, and Alpaca counts
money from a sale only once the sale has filled. So "never borrow" and "sell and buy at
the same open" can't both hold strictly. The owner chose to stay close to the backtest
and make any borrowing visible:

- Margin stays on in the paper account, so buys that rely on same-auction sales are not
  rejected.
- Targets are sized from equity minus a cash buffer (default 2%, editable, logged), so
  cash stays at or above zero unless prices gap up more than the buffer or a sale fails.
- Any day that ends with negative cash is flagged in the log, in the paper window and in
  fill tracking, with the amount.

### Whole shares (designer's call)

Orders are whole shares sized from day T's close, because the next open is unknown when
the order is sent. The backtest uses fractional shares sized from T+1's open. Fill
tracking shows the effect (see "Fill tracking"). Alpaca refuses fractional at-the-open
orders (checked 2026-11-13: HTTP 422, `fractional orders must be DAY orders`), so whole
shares are the only option.

## Strategy contract

Backtests load a strategy once and call it for every day; paper trading and Replay load
it fresh and call it once. These give the same targets only if the strategy's output
depends on nothing but `history` and `as_of`. This becomes part of the interface: no
state kept between calls, no unseeded randomness, no reading files, clock or network.
The app can't enforce it, so the README states it, and Replay is the check: a strategy
that breaks it shows up as a replay mismatch.

## Safety limits

Set when paper trading is first set up for an account, stored with each log entry, and
editable later (an edit is itself logged). All safety state (halt, loss lock, clears,
settings) is stored per account ID and endpoint, on disk, so a restart doesn't undo it
and switching keys to another account never applies one account's state to another.

- **Maximum position size:** a percent of account equity per symbol (default 25%).
  A buy that would take a symbol over the cap, valued at day T's close, is trimmed to
  the cap. The trim is shown before sending and logged. Sales are never trimmed.
- **Daily loss cutoff:** a percent (default 3%). It is checked for every trading session
  since the last one checked, not just day T, so a big loss on a day the app was closed
  is still caught. Each session's loss is its closing equity against the previous
  session's, from Alpaca's portfolio history (designer's call: Alpaca's `last_equity`
  moves overnight). The last session checked is stored. If any session's loss is larger
  than the cutoff, the run sends no buys and a red banner appears. If the history needed
  is missing or zero, buys are blocked too. Checking starts at the account's first run
  (designer's call after the Alpaca check: a new account's history shows equity 0 for
  every day before it was funded). On the first run only day T's session is checked, and
  a previous session at 0 counts as the funding day, not a loss. After that, a 0 in any
  session that needs checking blocks buys. Each history point is dated by its New York
  date; Alpaca stamps them 20:00 New York time. **The banner is a lock (owner's decision):**
  no run on any later day sends buys until a person clears it. Clearing records which
  session's loss was cleared, so a later breach locks again. Sales toward the targets
  still go out. Nothing is sold just because the cutoff was hit.
- **Kill switch:** one button, always enabled when an account is set up. It halts and
  cancels every open order (see "The journal and recovery"). Positions are left alone.
  **Sell everything** is a separate button with its own confirmation.

## Paper by default

- The trading client is always created with Alpaca's paper flag in this stage. The live
  option appears in the paper window as a disabled switch with the reason: the app has
  no code signing, so a tampered update could trade real money. Turning it on is a later
  design, with separate live keys.
- The app uses the Alpaca keys already stored in the keyring (paper keys work for both
  data and paper trading). Keys never go in the repo, the logs or exported files.
- At setup, the app checks the account is a paper account (by endpoint) and shows the
  account number so the user can confirm it is the right one. If the keys later point to
  a different account, any unsent plan is discarded and the user confirms the new
  account before anything runs.

## The log

One entry per run, stored on this computer next to the backtest runs, in a format the
builder picks that can be opened outside the app (for example a folder per day with
CSV and JSON files). The journal above is part of it. Each entry records:

- Account ID and endpoint, run time, day T, attempt, app version, the user's choice
  (sent, cancelled, failed) and any error text.
- The strategy file's path, a copy of its code, and its SHA-256 (saved runs keep only
  the copy today; the hash is new).
- The dividends setting for the strategy's input (on by default, matching the
  backtest's).
- **What the strategy saw:** a SHA-256 of the exact input passed to it (exact values,
  in a fixed column and row order), the data date, when that data was downloaded, and
  the last day's bars for every symbol it held or targeted. The full input is not copied
  (years of bars for about 500 symbols; owner's decision).
- Its returned targets.
- The account snapshot it worked from: equity, cash, buying power, positions.
- Every planned order, every trim and block with its reason, and every sent order with
  its `client_order_id`, Alpaca order ID and final state.
- Safety settings in force, and the loss check's result for each session checked.

**Replay:** a button on a log entry rebuilds the input from the stored daily data,
checks the hash matches exactly, and re-runs the saved strategy copy to show whether it
returns the same targets. If the hash doesn't match (the daily data was since
re-downloaded, corrected or re-adjusted), it says so rather than replaying on different
data. Known limit, accepted with the fingerprint choice: once stored data changes, an old
entry's exact input can't be rebuilt.

## Fill tracking

After the open on T+1, the next time the app is open it fetches the fills for orders
it sent (by `client_order_id`) and records, per order:

- Shares ordered and shares filled (an unfilled or partly filled at-the-open order is
  recorded as such).
- Average fill price.
- **Expected price:** T+1's official open from the daily bars, which is what the
  backtest assumes. Difference in basis points, signed so a positive number is always a
  cost (paid more on a buy, got less on a sale).
- Day T's close (the sizing price). Difference in basis points, signed the same way.

Price is not the only gap. For each session it also records the shares the backtest's
rules would have held after rebalancing (fractional, sized from T+1's open with the
same targets), next to the shares planned, sent and filled, and names the reason for
each difference: cash buffer, whole-share rounding, size cap, loss lock, split block,
unfilled order, or a missed session.

Alpaca's paper fills are simulated against quotes, so the difference from the official
open includes the simulator's behavior, not just real trading costs. The paper window
says so.

The paper window shows these per order and as averages weighted by traded value, so
trading costs can be compared with a backtest's `cost_bps`. Exportable as CSV.

## Performance in the compare screen

The paper account appears in the compare screen's run list as pinned entries (like
SPY), one per strategy (owner's decision): "Paper: <strategy name>". A strategy here is
a strategy file at a given SHA-256; editing the code starts a new line (designer's
call), and the old line stays listed.

- A line starts at the close of the session before its first orders filled, so the first
  day's return is counted, and it records the positions it inherited from the previous
  strategy, if any.
- Switching strategy takes effect at the first session the new strategy's orders fill;
  the old line ends at the close before it.
- Account history before the app's first sent run is ignored.
- Daily values come from Alpaca's portfolio history (daily equity), not from the app's
  own snapshots, because the app is not open every day. Deposits and withdrawals don't
  happen on a paper account; if the builder finds the history includes any, they are
  excluded from returns.

Alpaca's paper accounts don't receive dividends. So a paper line is marked "no
dividends (Alpaca paper)", and the window suggests comparing it with a backtest run
with dividends off. The strategy's own input keeps its dividends setting either way.

It gets the same rows as a run: total return, yearly growth, worst drop, volatility,
beta and alpha against SPY, each with its day count. The "best" marking ignores it, as
it ignores SPY.

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

- Interrupted sends resume the confirmed plan unchanged; a kill switch release starts a
  new attempt only after every old order is confirmed finished.
- Order IDs are built from account, day, attempt and symbol, so two plans for the same
  day collide at Alpaca. One computer per account at a time; the app warns otherwise.
- The daily loss cutoff checks every session since the last one checked, each against
  the session before, from portfolio history. Missing history blocks buys.
- A symbol with a split at T+1's open is blocked for that day; a failed split lookup
  stops the run.
- T and T+1 come from Alpaca's market calendar; weekend and holiday runs target the
  next trading day.
- Orders are worked out from Alpaca's real positions, not a ledger the app keeps. If
  someone trades by hand in the account, the next run corrects it toward the targets.
- Whole shares sized from day T's close (see "Whole shares").
- A plan is rechecked at Send and discarded if anything it relied on changed.
- Editing a strategy's code starts a new paper line in the compare screen.
- Daily data is refreshed as the run's first step, and its coverage is verified.
- Paper account performance comes from Alpaca's portfolio history.
- Default limits: 25% per symbol, 3% daily loss, 2% cash buffer. Editable.
- Kill switch leaves positions alone; selling is a separate confirmed button.
- Logs stay on each computer and are never synced.

## First build step

Several statements above about Alpaca come from memory, not from tests. Before anything
else, the builder checks them against a real paper account and reports to the owner:
fractional at-the-open orders, weekend submission, `client_order_id` reuse after a
cancel, looking up an order by `client_order_id`, listing a day's orders by ID prefix,
the market calendar, a corporate-actions query that reaches T+1, buying power for buys
alongside same-auction sales, that paper accounts get no dividends, and what portfolio
history returns for a new account. Anything that turns out different comes back to the
owner before the build relies on it.

**Results (live paper account, 2026-11-13 00:33 ET; owner approved the changes):**
- At-the-open order, lookup by `client_order_id`, cancel, listing a day's orders: as
  assumed. Paper account numbers look like `PA` plus 7 characters.
- A `client_order_id` can never be reused, even after a cancel. A duplicate and a reuse
  both fail with HTTP 422, code 42210000, `client_order_id must be unique`. This matches
  the new-attempt-new-IDs rule. The text can't tell "my own earlier send" apart from
  "another computer", so the order is always looked up by ID.
- Fractional at-the-open orders are refused (see "Whole shares").
- Portfolio history: one point per session at 20:00 New York time, equity 0 before
  funding (see "Daily loss cutoff").
- The market calendar lists sessions only. A corporate-actions query reaching T+1 works
  and returns announced future splits.
- Not checked: weekend submission, buying power alongside a pending sale (the account
  held nothing), a holiday in the calendar, paper dividends (taken from Alpaca's docs).

## Left to the builder

Module and function layout, log file format, how state (halt, banner, settings,
strategy path) is stored, the window layout, the exact wording of messages, and how
progress is shown, within the constraints above.
