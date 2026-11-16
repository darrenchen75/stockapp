# Interface Fixes (Feedback Part 2) Design

Date: 2026-11-16. Status: draft for owner approval (builder-checked). Branch `feat/interface`, based on
`feat/risk-stats` (head 5f7fde9). Building starts after the Task 11 part B live paper check
on `feat/risk-stats` has run and that branch is finished.

Source: the reviewer's feedback doc of 2026-11-05 ("Feedback on Stock Bars v0.1.3"), Part 2.
Part 1 is covered by earlier specs. Items 8 and 10 still wait on the reviewer.

## Intent

The main window (stock list on the left, minute chart on the right) becomes a quick market
overview for the reviewer: how stocks, sectors and the market are doing today, at a glance,
on data good enough to trust. The strategy lives in the Backtests and Paper windows. Part 2
does not touch them, apart from the theme.

The owner chose to do all of Part 2 now, before the reviewer sends the item 8 case. The
reviewer ranked Part 2 below Part 1 "until the data source is settled". This design settles
the data source for the minute chart, so Part 2 can go first.

## Goals

1. **Data coverage.** The minute chart uses Alpaca's full-market SIP feed instead of IEX.
2. **Chart clarity.** The chart gets a title and axis labels. A bold close-price line sits over
   faint candles. The mouse zooms and pans.
3. **Window buttons.** 1D, 5D and 30D always re-scope to the latest period, and the axis
   ticks fit that scale. Today, scrolling back and pressing 1D does nothing.
4. **Stats panel** under the chart, showing the selected symbol beside the overall market.
5. **Aggregated views.** A "Group by" control groups the list by sector, exchange or index.
   Company names are shown, not only tickers.
6. **Dark/light display** for every window.

## Non-goals

- Changing the backtest or paper universe. `daily.universe()` stays S&P 500 plus SPY. The
  extra symbols below are for the viewer only, so backtest and reconciliation numbers do not
  move. **This is an anti-goal: any change that alters backtest inputs is a bug.**
- Real-time full-market data. That needs a paid Alpaca plan, which the owner declined. The
  chart is 15 minutes behind.
- Delisted companies and historical index membership. Those need a paid source (later data
  stage).
- Moving averages or other indicators. Charts of a group's average are also out.
- Any change to Backtests or Paper behaviour.

## Owner's decisions (2026-11-16)

- Main window audience: the reviewer, for a market glance.
- Minute data: SIP with the free plan's 15-minute delay. On first launch after the update,
  the stored 30 days of IEX bars are replaced by SIP bars.
- Trendline: a bold line through each bar's close, with faint candles behind it.
- Zoom: the mouse wheel zooms time and dragging pans. The price axis refits to the visible
  bars. 1D/5D/30D, and a double-click on the chart, snap back to the latest period.
- Stats: a panel under the chart, with the selected symbol and the market side by side.
- Market: SPY's own figures.
- Price basis: regular session only (09:30-16:00 ET) for Last, % changes, Volume and
  volatility, in the list and in the panel. Extended-hours bars appear only on the chart.
- Volatility: standard deviation of daily returns over the last 30 days, scaled to a year.
  It is built from the stored minute bars.
- Grouping: a "Group by" control (none / sector / exchange / index). Group rows can be
  collapsed and show the group's average % change.
- Indexes: S&P 500, Nasdaq-100 and Dow 30, from member lists shipped with the app. Nasdaq-100
  and Dow 30 members that are not in the S&P 500 are added to the viewer's symbols.
- Group click: a sector or index group charts a matching ETF, and its stats fill the panel.
  An exchange group only expands or collapses.
- Theme: follows the OS by default. A System / Light / Dark choice overrides it and is
  remembered. It applies to all windows and charts.
- Delivery: one design and one plan, built in phases (below).

## Design

### Symbols

- `load_symbols()` keeps returning the S&P 500 list unchanged. `daily.universe()` and
  everything else in Backtests and Paper keep using it.
- A new viewer list is the union of three things: S&P 500, Nasdaq-100 and Dow 30 members,
  plus the chart ETFs. The ETFs are SPY, QQQ, DIA and the 11 sector SPDRs (XLK, XLF, XLV,
  XLY, XLP, XLE, XLI, XLB, XLU, XLRE, XLC). `main.py` passes this list to `MainWindow` and the
  poller.
- New shipped files sit next to `app/sp500.csv`: `nasdaq100.csv` and `dow30.csv`. Each has
  Symbol, Name and GICS Sector, so the non-S&P members still get a name and a sector. There
  is also an ETF table mapping each ETF to its name and to the group it stands for. That table
  can be a small constant in code. The README says where each list came from and its date.
  Lists are refreshed by hand.
- Exchange comes from Alpaca's asset list (`TradingClient.get_all_assets`, field `exchange`).
  It is fetched in the background and cached in `bars.db`, then refreshed at most once a
  week. Until it loads, the "exchange" grouping shows a single "Unknown" group. *(My call:
  the cache and refresh rate.)*
- *(My call.)* The lookup calls the **paper** trading endpoint only, read-only, like
  `make_broker`'s `paper=True`. It follows the existing rule of never touching a non-paper
  endpoint. If it fails, for example a 401 from a live-only key, exchanges stay "Unknown" and
  a note is logged. That failure must never reach `on_auth_failed` or prompt for new keys,
  because the data key still works.
- Ticker symbols use the form the existing CSV uses. Any form that differs in Alpaca (e.g.
  `BRK.B`) must be handled the same way it is for the S&P 500 today.

### Data: SIP feed, 15 minutes delayed

- `fetch` in `app/feed.py` asks for `DataFeed.SIP`. Every request ends at `now - 15 min`,
  since the free plan rejects SIP data newer than that. `app/daily.py` already does this.
  Backfill, the poller and `repair_splits` all follow this rule.
- First launch after the update: a marker in `bars.db` records the feed. When the marker is
  missing or says IEX, all minute bars are deleted and the 35-day backfill runs again from
  SIP. The marker is written only after the backfill completes, so an interrupted run starts
  over. The existing first-run progress UI is reused. The `splits` table stays, because SIP
  bars are split-adjusted the same way.
- The chart and the list header say "SIP, 15 min delayed". The status bar shows the time of
  the newest bar as before. Its "live" check (`LIVE_SECONDS` in `app/ui.py`) allows for the
  15-minute delay, so it doesn't always read "Market closed". *(My call.)*
- *(My call.)* Minute bars are kept for 35 days instead of 30: in `Store.prune`, the
  backfill floor in `feed.plan`, and the SIP re-download. That leaves a reference close for
  "Change 30D". The chart's 30D window and the Gaps column still cover 30 days. The first
  download grows by about a sixth.
- The Gaps column stays. With SIP it measures real missing data. *(My call.)*

### Chart

- A title gives the symbol, company name and range (e.g. "AAPL · Apple Inc. · 5D"). The axes
  are labelled: time on the bottom, in New York time, and "Price (USD)" on the left.
- The bottom axis is a date/time axis whose ticks fit the range: times of day for 1D, and
  days for 5D and 30D. Nights, weekends and holidays have no bars and are collapsed, so the
  line has no long flat gaps. *(My call: collapse them; how is left to the builder.)*
- A bold close-price line is drawn over faint candles. Up/down candle colours stay as they
  are, at low opacity. The line colour is theme-aware.
- The mouse wheel zooms the time axis only. Dragging pans in time. The price axis auto-fits
  to the bars in view.
- 1D/5D/30D buttons and a double-click reset the view to the latest period of the chosen
  range. They do this even after the user has zoomed or panned.
- When a new bar arrives and the user hasn't zoomed or panned, the view follows the latest
  bar. Once the user has zoomed or panned, the view stays put until a button or a
  double-click resets it. *(My call: refreshes must not yank the view away while someone is
  looking at an older part of the chart.)*

### Stats panel

The panel sits under the chart and has two columns: the selected symbol and SPY. When SPY
itself is selected, both columns show SPY. Rows:

| Row | Definition |
|---|---|
| Name | Company or ETF name |
| Last price | Latest regular-session bar's close: the newest bar at or before 16:00 ET (15 min delayed during the session; frozen at the 16:00 close afterwards) |
| Change 1D / 5D / 30D | % change from a reference regular-session close to the last price. 1D: the previous session's close. 5D/30D: see below |
| Volume | Shares traded 09:30-16:00 ET in the latest session |
| Volatility (30D) | Std of daily close-to-close returns over the last 30 days × √252, with the number of returns shown, e.g. "21.4% (20 d)" |
| Sector | GICS sector, "ETF" for ETFs |

- **Owner's decision: regular session only.** Last, % changes, Volume and volatility use
  regular-session bars (09:30-16:00 ET). A session's close is its last bar at or before
  16:00 ET. Pre- and post-market bars are left out of every number, so the figures match
  published quotes. They still appear on the chart. The same rule applies to the list's
  Last, % Chg and Volume and to the group averages. `Store.summary()` changes to this rule,
  so the list and the panel never disagree for the same symbol.
- *(My call.)* The 5D/30D reference is the close of the latest session whose date is on or
  before the date that many calendar days back, New York time. This matches the chart
  buttons. If there is not enough data, the cell is blank, never 0.
- The panel updates on selection and on each refresh.
- Volatility shows its sample size, following the reviewer's item 9 note about noisy
  estimates.

### List and grouping

- Columns: Symbol, Name, Last, % Chg, Volume, Gaps. Name is the company or ETF name.
- A "Group by" control above the list offers: None (today's flat list, the default), Sector,
  Exchange and Index.
- Grouped view: each group is a top-level row that can be collapsed. It shows the group
  name, its member count and the average % Chg of its members (equal weight, skipping
  members with no value). Members sit under it with the same columns. Sorting sorts members
  within each group. Groups are sorted by name.
- A stock can be in more than one index group (e.g. AAPL is in all three). It appears under
  each.
- ETFs appear in the flat list. In grouped views they are not listed as members. They back
  the group rows instead.
- Clicking a sector or index group row charts its ETF: Sector maps to the SPDR, S&P 500 to
  SPY, Nasdaq-100 to QQQ and Dow 30 to DIA. The stats panel fills with that ETF. Clicking an
  exchange row only toggles it.
- The chosen grouping and the expanded groups are remembered between launches. *(My call.)*

### Theme

- Default: follow the OS (Qt 6.12 `QStyleHints.colorScheme`, with live changes followed).
- Override: a View menu choice of System / Light / Dark, applied with
  `QStyleHints.setColorScheme`. It is remembered between launches.
- It covers every window: main, Backtests (compare), Paper, and their dialogs. pyqtgraph
  plots get matching backgrounds, axis, text and line colours. Fixed colours in code, like
  `UP`/`DOWN` in `app/ui.py` and `COLORS` in `app/compare_ui.py`, are checked for contrast
  on both backgrounds and get a dark-theme variant where needed.
- *(My call.)* Display preferences (theme, grouping, expanded groups) live in one small JSON
  file in the app's data directory, written atomically (temp file then `os.replace`), as the
  paper code does. A missing or corrupt file means defaults.

## Phases

Each phase can be reviewed and shipped on its own, in this order:

1. **Chart and theme:** range-button fix, zoom/pan/double-click, axis labels and title, close
   line over faint candles, gap collapsing, theme in every window.
2. **SIP switch:** feed change, the 15-minute end, the feed marker and re-download, labels.
3. **Names and stats:** Name column, member lists, viewer symbol list (Nasdaq-100/Dow 30
   extras and ETFs added to the poller), stats panel.
4. **Grouping:** Group-by control, exchange cache, group rows, ETF charts on group click,
   remembered grouping.

## Testing

- Unit tests for every new calculation: the 1D/5D/30D changes (regular-session close,
  calendar lookback, too little data gives a blank), volatility and its sample size, group
  averages (skipping blanks), the regular-session rule in `Store.summary()` (post-market bars ignored), the 35-day keep period, the viewer symbol union (no duplicates, `load_symbols()`
  unchanged) and the feed marker re-download (including an interrupted re-download).
- A test that `daily.universe()` is unchanged. This guards the anti-goal.
- `fetch` sends SIP and an end at least 15 minutes old (fake client, as in `tests/test_feed.py`).
- UI tests in the existing offscreen-Qt style (`QT_QPA_PLATFORM=offscreen`, as in `tests/test_compare_ui.py`): a range button resets a panned view, grouping
  toggles, a group click charts its ETF, and the theme switch reaches every open window.
- One live check (skipped unless `STOCKBARS_LIVE=1`, like the other live tests): SIP minute
  bars for a few symbols ending 15 minutes ago come back non-empty, and the asset list has
  an exchange for each viewer symbol.
- Owner looks at the main window in both themes on a real screen before the branch is
  finished.

## Left to the builder

- The widget for the grouped list (tree model vs proxy) and how group rows are styled.
- How nights and weekends are collapsed on the time axis.
- Exact line widths, opacities and theme colours, within readable contrast.
- Where the ETF table and member lists are loaded, following `load_symbols()`'s pattern.

## Risks

- **Bigger first download:** about 530 symbols × 35 days of SIP minute bars, which includes
  extended hours. The existing batching (`MAX_SPAN`) bounds memory. Expect a longer first
  launch than today.
- **Stale member lists:** index changes are not tracked automatically. The README dates each
  list.
- **Universe leak:** if a builder routes the viewer list into `daily.universe()`, backtests
  change silently. The unchanged-universe test guards this.
