# Backtest Compare Screen Design

Date: 2026-11-08. Status: design approved for planning. Building waits for
reconciliation (see "Gate").

Builds on `docs/superpowers/specs/2026-11-06-strategy-backtest-design.md` (the
module spec). Its "App screen" section is replaced by this document. The owner picked
option 5, "Compare runs", from five mockups:
https://claude.ai/artifact/JFASe9y4XPGTsoCGL3LSS1

## Intent

The owner runs strategies more than once and wants to see the runs side by side, to
judge which one holds up. The owner isn't yet sure what will usually change between
runs (a different strategy file, an edited version of the same file, or different
settings), so the screen must handle all three.

This screen also absorbs the module spec's later "performance page". The module spec
kept the app screen minimal and left charts and statistics to that page. The owner
chose to merge them.

## Gate

**Lifted by the owner on 2026-11-08.** The screen is built before the backtest has been
matched against the reviewer's real case (module plan, Task 8). If Task 8 changes the
shape of results, saved runs are handled through the results format version (see
"Saved runs"), and the screen is adjusted then.

## Goals

- Run a backtest from inside the app, in the background, with progress.
- Keep every run on this computer, with enough recorded to know exactly what produced
  it.
- Compare up to 6 runs on one chart and in one table.
- Open one run to see its holdings at each rebalance and its warnings.
- Export a run's results as the same two CSVs the command writes.

## Non-goals

- Syncing runs between computers. Each computer keeps its own (owner's decision).
  The app runs on several computers and operating systems. Daily data is downloaded
  separately on each (about a minute for the full universe).
- Importing runs from another computer.
- Editing strategies inside the app.
- Optimization or parameter sweeps.
- Any order placement, paper or live.

## Anti-goals

- A screen whose numbers differ from the command's for the same inputs.
- A run that can't later be traced to the exact strategy code and settings that made
  it.
- Changes to the existing main window layout or the minute viewer's behavior.
- A frozen window while a download or run is in progress.

## The screen

Opened from the main window without changing its existing layout. The exact entry
point (menu item, toolbar button) is left to the builder.

- **Run list (left).** Every saved run, newest first. Each row shows the run's name
  (the strategy file name by default), its date range, its cost setting and a
  checkbox. A ticked run gets a color swatch matching its chart line. At most 6 runs
  can be ticked at once; ticking a seventh is refused with a short message. SPY
  appears as a benchmark entry that can be ticked like a run (dashed gray line).
  Runs can be renamed and deleted (delete asks for confirmation).
- **Chart (main).** pyqtgraph, which the viewer already uses. Each ticked run is
  plotted as the growth of $100,000: its value is scaled to start at $100,000,
  whatever its own capital, so runs with different capitals compare directly. A
  toggle switches to "drop from previous high", which is unaffected by the scaling.
  The x axis is dates. Runs with different date ranges are plotted on their own
  dates, unaligned.
- **Comparison table (below the chart).** One column per ticked run, and one row
  each for total return, yearly growth (CAGR), worst drop (maximum drawdown),
  rebalances and warnings. Bold marks the best value in each row. Best means highest
  for return and growth, closest to zero for worst drop, and is not marked for
  rebalances or warnings.
- **New run.** "New…" opens a small dialog with the fields from mockup option 1:
  strategy file, start date, end date, starting capital, cost in basis points, and
  include dividends. "Re-run" on a selected run opens the same dialog pre-filled
  from that run.
- **Running.** The run happens off the UI thread. The dialog or screen shows
  progress: the download batches, then the run's days. A Cancel button stops it.
  - A cancelled run is not saved.
  - A strategy error shows one message naming the failing date, and the failed run
    is not saved.
  - A download failure shows one message and saves nothing.
  - One run at a time: "New…" and "Re-run" are disabled while a run is in progress.
  - Closing the window during a run asks for confirmation, then cancels the run.
  - The update offer waits until no run is in progress, the same way it already
    waits for other busy states.
  - Cancel works at every stage: between download batches, between run days, after
    the last day, and just before the saved run is published. The screen stays busy
    until the worker has actually stopped.
  - The strategy file's bytes are read once, before anything runs. That exact code
    is executed and saved, so editing or deleting the file mid-run changes nothing.
  - `run()` gains two optional callbacks, one for progress and one that is checked
    for cancel at each day. The defaults keep the command's behavior unchanged.
  - A worker thread is acceptable. The builder may use a subprocess instead if a
    thread makes the window sluggish, but must keep the identical-results test
    passing.
- **Run detail.** Double-clicking a run opens its detail: holdings at each rebalance
  (symbol, target weight, fill price, shares, value, note), and its warnings, as in
  mockup option 4's Positions and Warnings tabs.
- **Export.** Writes `portfolio.csv` and `positions.csv` for a run, through
  `write_results`, to a folder the user picks.
- **Benchmark.** SPY buy-and-hold, computed with the same `run` function and a
  buy-SPY strategy defined inside the app. `tests/fixtures/buy_spy.py` does not ship
  in the packaged app.
  - Its settings: $100,000 capital (the chart's base), 0 bps cost, dividends on.
  - Its dates: the earliest start through the latest end among the ticked runs. With
    no run ticked, the chart shows a hint to tick a run, and nothing is plotted.
  - Its data: recomputed from the current daily data whenever it is shown. The
    legend shows the data date it used. It is not saved.
  - It is computed off the UI thread and cached by data date and date range. A
    result for a selection that has since changed is discarded. Run lines show at
    once, and the SPY line follows.
  - With no daily data on this computer yet, the SPY entry is disabled, with a hint
    to run a backtest first.
  - It doesn't count toward the 6-run limit.
  - It gets a table column, but is never marked best.
- **Results note.** The survivorship-bias, data source, dividend setting and
  known-gaps note from the module spec appears on the screen, in the run detail and
  in exports.

## Saved runs

- **Where.** A `runs` folder inside Stock Bars' own data folder (the same
  `user_data_dir` that holds `bars.db`). This is per computer.
- **What each run keeps.**
  - Its settings: start, end, capital, cost_bps and dividends.
  - The run's name, and the time it ran.
  - A byte-for-byte copy of the strategy file as it was when run, with its original
    path.
  - The results: the same data `write_results` writes, so export can copy them.
  - The data date it ran on (the `daily_through` marker at run time).
  - The app version.
  - A results format version.
    - Task 8 may still change the shape of `Result`.
    - A run saved under an older format stays listed, marked "older format".
    - Its chart line and statistics are shown only if its saved columns still
      support them. Otherwise it offers Re-run.
    - No silent conversion.
- **Format.** Left to the builder: plain files per run (a folder with a settings
  file, the strategy copy and the two CSVs) or tables in a SQLite file. If SQLite,
  it must not be `bars.db`'s minute tables, and it must coexist with the viewer the
  same way the daily table does.
- **Statistics** (total return, yearly growth, worst drop, rebalances, warnings) are
  computed from the saved results whenever they are shown. They are never stored, so
  old runs never need re-running if a formula is fixed.
- **Privacy.** The strategy copies stay on this computer. Nothing uploads them.

## Same numbers as the command

The screen calls the module's existing functions (`load_strategy`, `refresh_daily`,
`load_bars`, `run`, `write_results`). It must not reimplement them. A test confirms
that the screen's run path and `python -m app.backtest` produce identical
`portfolio.csv` and `positions.csv` for the same strategy, dates and settings.

The statistics are defined once, in the module, so the command can print the same
figures later:

- **Total return:** final value / starting capital − 1.
- **Yearly growth (CAGR):** (final / start) ^ (365.25 / calendar days) − 1. It is
  unavailable, shown as "—" and never marked best, when a run spans zero calendar
  days.
- **Worst drop:** the minimum over days of value / running maximum − 1.
- **Rebalances:** the number of decision days with at least one trade.
- **Warnings:** the number of position rows with a warning.

## Packaging and platforms

The packaged app can import only what PyInstaller bundles. Update the build flags in
`.github/workflows/release.yml`, which releases use, and keep `StockBars.spec` in step.
A strategy file must be able to import pandas, numpy and the standard library.
PyInstaller bundles only the standard-library modules it detects, so collect them
explicitly. Check this in a built app on each OS the release targets. Strategy
files are loaded by path at run time, as the command does.

## Documentation

Update `README.md` to describe the screen. Its statement that the app never buys or
sells stays true.

## Calls made by the designer (owner to check)

| Topic | Call |
|---|---|
| Rename and delete runs | Yes; delete asks for confirmation |
| Most runs on the chart at once | 6 |
| Statistics | Computed from saved results each time, never stored |
| Failed runs | Not saved |
| SPY benchmark | Tickable entry; $100k, 0 bps, dividends on; recomputed live; not saved; outside the 6-run limit; never marked best |
| Different date ranges | Each run plotted on its own dates |
| Best-value marking | Return, growth and worst drop only |
| Chart scale | Every line scaled to start at $100,000 |
| Cancel | Yes; cancelled runs are not saved |
| Close or update during a run | Close asks, then cancels; the update offer waits |
| Worker | Thread by default; subprocess allowed if the window gets sluggish |
| Older saved runs | Kept and marked; shown if still readable, else Re-run |

## Left to the builder

- The exact entry point from the main window.
- The saved-run format (plain files or SQLite), within the rules above.
- Whether progress shows in the dialog or the screen.
- Colors for up to 6 lines. They must differ in lightness as well as hue, and match
  the app's existing look.
- Window sizing and layout details within the mockup's arrangement.
