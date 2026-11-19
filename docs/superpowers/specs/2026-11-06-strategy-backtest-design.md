# Daily Data and Strategy Backtest — Design

Date: 2026-11-06

## Intent

Stock Bars is a read-only viewer of minute prices. The goal is to turn it into a
strategy and trading system, following an outside reviewer's feedback on v0.1.3
(`Feedback on Stock Bars v0.1.3 Strategy Shell Requirements.pdf`).

This design covers only the first stage: enough to test a daily strategy on years
of history and prove the results match the reviewer's research notebook. Orders
depend on trustworthy numbers, so nothing here places one. Paper trading gets its
own design at the decision point below.

This stage covers feedback items 1 (historical data), 2 (one strategy interface),
7 (timing rules) and 8 (reconciliation). Items 3, 4, 5, 6, 9 and 10 and all of
Part 2 come later.

## Goals

1. Several years of daily bars stored locally, unaffected by the 30-day minute prune.
2. One strategy interface that paper and live trading can reuse without a rewrite.
3. Timing rules enforced by the runner: a strategy cannot see data it would not
   have had on that day.
4. A backtest runnable from the command line or a notebook, producing a results CSV.
5. A reconciliation check that passes on the reviewer's test case.
6. After that passes, a simple app screen that gives the same results as the module.

## Non-goals

- Placing or simulating broker orders, paper or live.
- Safety limits, decision logs, fill tracking, oversight features (paper stage).
- The performance page: volatility, drawdown, beta, alpha (next stage).
- Paid data, delisted companies and historical index membership.
- Part 2 interface fixes.
- Short positions, leverage or intraday strategies, unless the test case needs them.

## Anti-goals

This has failed, even if it works, if any of these is true:

- Backtest logic and later live logic would need separate versions of a strategy.
- A strategy can see a price from after its decision time.
- A real strategy ends up in the repo or in a release file.
- Results are presented as unbiased while the stock list has survivorship bias.
- Existing viewer behavior changes: startup, minute data, or update flow.

## Decisions made by the owner

- Commit through reconciliation only. Paper trading is decided afterwards.
- The repo stays public for now. Going private would need a public releases repo
  and a migration release first, because `app/update.py` reads public GitHub
  releases without logging in.
- The real strategy is a private local file loaded by path. The repo holds only the
  interface, the runner and a dummy strategy.
- Build the module first, then the app screen, both before the decision point.
- On each day T, a strategy sees history adjusted only for events up to T, so backtest
  and live runs see the same thing. Fully back-adjusted prices are not acceptable.
- No live trading is planned. It would first need the update path hardened, because
  the app has no code signing.

## Calls made by the designer (owner to check)

These follow from the discussion but the owner did not specify them. Several may
change once the reviewer answers (see "Waiting on the reviewer").

| Topic | Call |
|---|---|
| History | From 2016, the earliest Alpaca offers on the free plan |
| Feed | Alpaca SIP daily bars; never request bars newer than 15 minutes |
| Stocks | Today's `app/sp500.csv` plus SPY |
| Strategy output | Target weight per symbol, long or flat |
| Timing | Decide at 20:30 New York time on day T, fill at day T+1's open |
| Unaffordable purchases | Scale all purchases by one common factor; never borrow |
| Costs | One cost setting in basis points of traded value, default 0 |
| Dividends | Included in returns; one setting switches to splits only |
| Adjustment method | Raw prices plus per-day factors built from Alpaca corporate actions (see "Data") |
| Results | CSV files |

## Data

- Add a daily bars table in `bars.db`, separate from the minute `bars` table.
  `Store.prune` must not touch it. A separate database file is acceptable if the
  builder finds a reason.
- The download is triggered by a backtest or an explicit command, never by app
  startup. People who only use the viewer pay no download cost.
- Reuse the app's stored key (`app/settings.py`), the retry handling in
  `app/feed.py`, and the SQLite WAL setup, since the app may run at the same time.
- The minute feed stays on IEX. This design does not change the viewer's data.

**Prices as of each day.** Fully adjusted history rewrites old prices using later
splits and dividends, so a strategy on day T would see the effect of events after T.
To prevent that:

- Store raw daily bars (`Adjustment.RAW`), which never restate, plus two per-symbol,
  per-day cumulative factors built from Alpaca corporate actions (forward splits,
  reverse splits and cash dividends):
  - Split factor: the product of `old_rate / new_rate` over splits with an ex-date
    after the day.
  - Full factor: the split factor times the product of `1 - d / C` over cash dividends
    with an ex-date after the day. `d` is the amount as paid and `C` is the raw close
    on the last trading day before the ex-date. Records sharing an ex-date multiply.
    Every cash dividend counts, because Alpaca's `special` flag is unreliable.
  - Both factors are 1 on and after a symbol's last event.
  - Alpaca's adjusted bars are not used. They are rounded to 2 decimals, including
    split-adjusted closes before a split, which makes derived factors noisy.
  - Other action types, such as spin-offs and stock dividends, are ignored for now.
- For a decision on day T, the runner gives the strategy each earlier day t's price as
  `raw(t) * factor(t) / factor(T)`, so day T's own price equals what traded that day.
- Adjust volume for splits only, on the same as-of-T basis. Dividends must not change it.
- Compute portfolio returns on one consistent adjusted basis, so dividends count when
  the dividend setting is on. Record actual raw prices as fill prices in the results.

**Factors can go stale.** A new split or dividend changes a symbol's factors. Replace
all of a symbol's factors in one transaction, and never run on a mix of old and new.
The builder chooses when to refresh them (for example, on a new corporate action, or
before a run when they are out of date).

**Verify first.** Both of these come from documentation or reasoning and have not been
tested with a real key. The first build step is a real request for several years of
SIP daily bars, raw and adjusted:

- Free-plan SIP access. If it fails, stop and return to the owner, because the data
  source decision is then open again.
- Factor quality. Alpaca's rounding of adjusted prices could add noise to derived
  factors. If that noise would matter at the reconciliation tolerance, build the
  factors from Alpaca's corporate actions data (already used in `app/feed.py`) instead.

Findings, 2026-11-08 (AAPL, F, SPY):

- Free-plan SIP works. RAW, SPLIT and ALL each returned 2728 bars from 2016-01-04,
  with matching dates. Daily timestamps are midnight New York time, so convert to New
  York time before taking the date.
- Factor quality failed. ALL/RAW moved by up to 4.0e-5 (AAPL) and 7.2e-4 (F) between
  dividends, and SPLIT/RAW by about 2e-5 before a split (AAPL 2020-08-28: 124.81
  against 499.23 / 4). The owner chose factors built from corporate actions. Built
  that way, the factors are exactly constant between events. They reproduce Alpaca's
  ALL closes to within $0.005 for AAPL and F.

**Known data gaps.** Alpaca's SPY dividends for 2016-03-18 and 2018-06-15 are missing
from both corporate actions and the ALL bars. For 2026-03-20, corporate actions says
1.797 while the ALL bars imply about 1.829. No trusted second source exists, so nothing
is corrected by hand. Results name the gaps, and a live check fails if they change.
Reconciliation shows whether they matter.

## Strategy interface

A strategy is a Python file defining one function. The exact signature is left to
the builder, but it must have this shape:

```python
def target_weights(history, as_of):
    """history: daily bars for all symbols through as_of's final bar, and nothing later,
    adjusted only for events up to as_of.
    Returns {symbol: weight}. Omitted symbols are held at zero."""
```

- The function receives data only as arguments. It gets no API keys, no clients and
  no database handle, so the same function can later run live.
- The runner validates each result: symbols present in the run's data, weights between
  0 and 1, total at most 1. Invalid output stops the run with an error that names the
  date.
- Strategy files run as ordinary code with the user's permissions. The app loads only
  a file the user picks and never fetches one.

## Backtest runner

The runner takes its bars as an input rather than reading the database directly, so
it can run on stored Alpaca data or on prices supplied by a test case. Designer's call.

For each trading day T in the chosen range, with trading days taken from SPY's bars
(or from the supplied prices' dates, if they have no SPY):

1. At day T's decision time (below), give the strategy history through day T and get
   target weights.
2. At T+1's open, rebalance toward those weights under the cash rules below.
3. Value the portfolio at T+1's close. Uninvested cash earns nothing.

**Decision time.** 20:30 New York time on day T. Designer's call. Extended-hours trading
has ended by then and the 15-minute SIP limit has passed, so every field of day T's bar
is final, including daily volume, which can include extended-hours trades. This holds
on early-close days too. A future live run uses the same cutoff. A day's bar is never
given to a strategy before that day's cutoff, so an unfinished session is excluded.

**Cash rules.** Every rebalance pays for itself, and cash never goes negative:

1. Size targets from portfolio value at T+1's open, using the last known price for any
   holding with no T+1 bar.
2. Run sales first and add the proceeds, net of costs, to cash.
3. Run purchases from that cash. If purchases and their costs exceed it, scale all
   purchases by one common factor so cash ends at exactly zero, and record a warning.

With zero costs and every symbol tradable, a target totalling 1 needs no scaling.

Rules for the awkward cases:

- A held symbol with no bar on a day keeps its last price and position, and cannot be
  sold that day. It still takes up capital, so purchases are limited by the cash rules.
  Record a warning in the results.
- A target for a symbol with no T+1 bar is not filled that day, and is retried on the
  next day's cycle only if the strategy still asks for it. Record a warning.
- Fractional positions are allowed.
- The last day in the range makes a decision with no fill.

Entry points:

- Command: `uv run python -m app.backtest <strategy.py> --start ... --end ... --out ...`
- Notebook: import the same function from `app.backtest`.

Results are one CSV of daily portfolio value and return, and one CSV of daily target
weights, fill prices and positions per symbol. Both must carry a visible note of the
data's survivorship bias, its feed, its dividend setting and its known dividend gaps.

## Reconciliation

- Add an automated pytest check using the reviewer's test case, with a tolerance on
  daily returns of 1e-6 unless the reviewer states one. The tolerance applies only when
  both sides use identical prices. Designer's call: if the notebook used another data
  source, run the case on the notebook's prices and report any Alpaca price differences
  separately, so a data-source difference cannot read as a runner failure.
- If the case's prices are its own, the stored Alpaca data is not proven by it. Add a
  separate check of stored data against a few known splits and dividends. If the case
  relies on Alpaca data, freeze a snapshot of those bars for the check, because refetches
  can restate adjusted history.
- Until the case arrives, use a small hand-computed synthetic case and a dummy
  strategy, so the runner is proven independently.
- Add a look-ahead test: changing prices, or adding a split or dividend, after day T
  must not change any decision made on or before T.
- Add a timing test: before day T's 20:30 New York cutoff, day T's bar is not given to
  the strategy.
- Add cash tests: a blocked sale followed by a replacement purchase, and a 100% target
  with nonzero costs. Cash must never go negative in either.
- Before committing the reviewer's case, confirm it is safe for a public repo. If
  not, keep it in a local file and skip the test when the file is absent.

This stage's module work is finished only when the reviewer's case passes.

## App screen

**Replaced 2026-11-08** by `docs/superpowers/specs/2026-11-08-backtest-compare-screen-design.md`.
The owner chose a compare-runs screen that also absorbs the performance page. The text
below is kept for history.

The owner chose to build this once reconciliation passes, so it waits for the reviewer's
real case. The synthetic case does not unlock it. Plan it separately, as a second plan
started when that case passes. It stays minimal:

- A way to open it from the main window, without changing the existing layout.
- Choose a strategy file and a date range, run it, and export the same CSVs.
- Run off the UI thread, show progress, and show strategy errors with the failing date.
- No charts or statistics; those belong to the performance page.

The packaged app can only import libraries bundled into it. pandas and numpy are
already locked through `alpaca-py`, but PyInstaller bundles only what it detects
(`.github/workflows/release.yml`). Update the build flags so a strategy can import
pandas, numpy and the standard library, and check this in a built app on each OS.

A test must confirm the screen and the module give identical results for the same
strategy and dates.

Update `README.md` to describe the screen. Its statement that the app never buys or
sells stays true at this stage.

## Waiting on the reviewer

The module build does not wait for these. Each has a default above, and an answer may
change it. The module's final check and the app screen do wait for the test case.

| Question | Default until answered | What changes |
|---|---|---|
| Test case, and whether it brings its own prices | Synthetic case | Final pass/fail check, data check |
| Dividends included in returns? | Yes | Dividend setting |
| Decision time, fill and cost assumptions | 20:30 New York on T, next open, zero cost | Runner steps 1 and 2 |
| Weights or shares, shorts allowed? | Weights, long or flat | Interface and validation |
| Meaning of "executive oversight" | Not in this stage | Paper-trading scope |
| Source for delisted stocks and index membership | None | Later data stage |

## Decision point

Once the reviewer's case passes and the screen matches the module, stop and return
to the owner. These must then be settled in a new design for paper trading:

- Where the strategy runs, since a desktop app works only while it is open.
- What the oversight features are.
- Whether the paid data comes first, since results are biased until then.

## Left to the builder

Table schema, function signatures, module layout, how the download reports progress,
CSV column names, when factors refresh, and where the screen is opened from.
