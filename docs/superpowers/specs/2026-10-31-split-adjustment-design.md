# Split-Adjusted Bars — Design

Date: 2026-10-31

## Problem

`app/feed.py` fetches raw (unadjusted) IEX minute bars incrementally into SQLite.
On a stock split, stored history keeps the old price scale and new bars use the
new one, so a 2-for-1 split shows as a fake 50% crash on the chart. The app will
later feed these bars to a trade-recommendation algorithm, which would misread
that drop as a real move.

## Goals

- Charts (and later the algorithm) see split-adjusted prices: one continuous line
  across a split.
- Existing caches with raw bars get fixed on upgrade, with no user action.

## Non-goals

- Dividend adjustment. Dividends cause small, real drops; adjusting would refetch
  about 8 symbols every trading day. (User decision.)
- Spinoffs, mergers, symbol renames.

## Design

- Every bar fetch passes `adjustment=Adjustment.SPLIT`, so new bars are on the
  same scale as refetched history.
- Split detection uses Alpaca's corporate-actions endpoint (`CorporateActionsRequest`,
  alpaca-py 0.44.0), types forward and reverse splits, ex-date in the last 30 days.
  It runs on launch and again whenever the New York date changes: about 1 request a day.
  (User decision.)
- For each S&P symbol with a split not yet handled, refetch that symbol's full 30 days,
  then replace in one SQLite transaction: delete all of the symbol's rows, insert the
  refetched bars, and record `(symbol, ex_date)` in a new `splits` table. A plain upsert
  is not enough: any cached minute the response omits would keep its raw price next to
  adjusted ones. The UI never sees a half-repaired or blank symbol.
- Empty refetch: commit nothing, leave the split pending, log a warning, retry on the
  next daily check. A crash mid-refetch also leaves it pending.
- Partial refetch (non-empty but missing minutes): commit anyway. Mixed scales are
  impossible after the delete; the cost is missing minutes, which the gap flag (next
  piece of work) makes visible. A strict coverage check was rejected: it compares two
  IEX responses and could leave a split pending forever on a minor data correction.
- Upgrade is automatic: the first run has an empty `splits` table, so any split in the
  existing 30-day raw cache gets refetched.

## Calls made by Claude (not user-decided)

- Query a rolling 30-day window each day rather than "since last check", to catch
  splits Alpaca posts late. Cost: a slightly larger request.
- Corporate-actions access on the free plan is assumed; the first implementation step
  verifies it with a real key.

## Tests

- Split repair replaces every cached row of the symbol, including minutes missing from
  the response, and records the split in the same transaction.
- Empty response: cache unchanged, split still pending.
- Already-handled split: no refetch.

## Scope

Small change: `app/feed.py`, `app/store.py`, tests. Built test-first.
