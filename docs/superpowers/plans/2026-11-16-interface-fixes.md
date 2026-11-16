# Interface Fixes (Feedback Part 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the main window into a trustworthy market overview: SIP data, a clearer chart, a stats panel, grouped lists with names, and a dark/light theme.

**Architecture:** New pure modules hold the logic (`sessions`, `quotes`, `listing`, `groups`, `prefs`); thin Qt modules (`theme`, `chart`) and `ui.py` render them. Network lookups for the market calendar and exchanges live in `market.py`, use the paper trading endpoint only, and run on the poller thread with failures logged, never reported as a bad key. The backtest and paper code is untouched except for theme colours.

**Tech Stack:** Python 3, PySide6 6.12, pyqtgraph 0.14, alpaca-py, SQLite, numpy, pytest (offscreen Qt).

**Spec:** `docs/superpowers/specs/2026-11-16-interface-fixes-design.md`

## Global Constraints

- `load_symbols()` and `daily.universe()` return exactly what they return today (S&P 500; S&P 500 + SPY). Any change to backtest or paper inputs is a bug.
- Minute data: `DataFeed.SIP`; every request ends at or before `now - 15 min`.
- Keep minute bars 35 days (`KEEP_DAYS = 35`); chart 30D window and the Gaps column stay 30 days.
- Regular-session figures only: a bar is regular when `session.open <= bar.ts < session.close` (`Bar.ts` = bar start). Pre/post-market bars count in no number; they are drawn on the chart.
- Missing data shows as a blank cell, never 0.
- `market.py` builds `TradingClient(key, secret, paper=True)` only. Its failures are logged; they never call `on_auth_failed` or prompt for keys.
- Display preferences live in `<data_dir>/display.json`, written temp file + `os.replace`; missing or corrupt = defaults.
- Chart ETFs: `SPY, QQQ, DIA, XLK, XLF, XLV, XLY, XLP, XLE, XLI, XLB, XLU, XLRE, XLC`.
- Labels: chart/list feed label `"SIP, 15 min delayed"`; during migration `"IEX, switching to SIP…"`.
- Run tests with `uv run pytest -q`. UI tests set `QT_QPA_PLATFORM=offscreen` like `tests/test_compare_ui.py`.

## Review Focus

1. **App opened before today's open, or on a weekend/holiday:** Last and 1D show the previous session vs the one before; nothing reads 0 or blank for a stock that traded. → test in Task 6.
2. **Early-close day (13:00 close):** post-13:00 bars excluded from Last/Volume/volatility. → test in Task 2 and Task 6.
3. **Migration interrupted by quit mid-download, then relaunched:** IEX bars still shown, resumes from staging, never an empty list. → test in Task 5.
4. **User scrolled back while new bars arrive and the oldest day is pruned:** the view still shows the same times. → test in Task 3.
5. **Live-only Alpaca key (paper endpoint 401):** calendar falls back, exchanges stay "Unknown", no key prompt, data keeps flowing. → test in Task 2 and Task 9.

---

## Phase 1: Chart and theme

### Task 1: Display preferences and theme

**Files:**
- Create: `app/prefs.py`, `app/theme.py`, `tests/test_theme.py`
- Modify: `app/main.py` (apply theme after `QApplication`), `app/ui.py` (View menu), `app/compare_ui.py` (plot styling + run colours follow theme)

**Interfaces:**
- Produces:
  - `prefs.load(data_dir: Path) -> dict` — `{}` on missing/corrupt file.
  - `prefs.save(data_dir: Path, **changes) -> None` — merge into current file, atomic write.
  - `theme.CHOICES = ("system", "light", "dark")`
  - `theme.Colors` frozen dataclass: `background, foreground, line, up, down: str` (hex).
  - `theme.apply(app: QApplication, choice: str) -> None`
  - `theme.colors() -> theme.Colors` — from the *effective* palette (window colour lightness < 128 → dark set).
  - `theme.shade(hex_color: str) -> str` — dark-theme variant for fixed colours, identity in light.
  - `theme.watcher() -> theme.Watcher` — app-wide singleton `QObject` with `changed = Signal()`, emitted on `QEvent.ApplicationPaletteChange` (event filter on the app), not on `colorSchemeChanged`.
  - `theme.style_plot(plot: pg.PlotWidget) -> None` — background, axis pens and text from `colors()`.

- [ ] **Step 1: Write failing tests** in `tests/test_theme.py`:

```python
def test_prefs_roundtrip_and_corrupt(tmp_path):
    prefs.save(tmp_path, theme="dark"); prefs.save(tmp_path, group_by="sector")
    assert prefs.load(tmp_path) == {"theme": "dark", "group_by": "sector"}
    (tmp_path / "display.json").write_text("{nope")
    assert prefs.load(tmp_path) == {}

def test_fallback_sets_fusion_palette_when_override_ignored(qapp, monkeypatch):
    monkeypatch.setattr(theme, "_override_took", lambda requested: False)
    theme.apply(qapp, "dark")
    assert qapp.style().name().lower() == "fusion" and theme.colors() == theme.DARK
    theme.apply(qapp, "system")
    assert qapp.style().name().lower() != "fusion" or theme.ORIGINAL_STYLE == "fusion"

def test_watcher_fires_on_palette_change_and_new_window_starts_in_new_colours(qapp, monkeypatch):
    monkeypatch.setattr(theme, "_override_took", lambda requested: False)
    seen = []; theme.watcher().changed.connect(lambda: seen.append(1))
    theme.apply(qapp, "dark"); qapp.processEvents()
    assert seen
    plot = pg.PlotWidget(); theme.style_plot(plot)
    assert plot.backgroundBrush().color().name() == theme.DARK.background
```

(`qapp` = a module fixture returning `QApplication.instance() or QApplication([])`.)

- [ ] **Step 2: Run** `uv run pytest tests/test_theme.py -v` — FAIL (no module).

- [ ] **Step 3: Implement `app/prefs.py` and `app/theme.py`.** `apply`: remember the original style name and palette on first call (`ORIGINAL_STYLE`); `"light"/"dark"` → `styleHints().setColorScheme(Qt.ColorScheme.Light/Dark)`, then if `not _override_took(requested)` → `app.setStyle("Fusion")` + a full light/dark `QPalette`; `"system"` → `setColorScheme(Qt.ColorScheme.Unknown)` and restore original style and palette. `_override_took(requested) -> bool` compares `styleHints().colorScheme()`. `shade` maps at least `#7b3fb0 → #b388ff`, `#3a6bff → #6f94ff`, `#c62828 → #ef9a9a`; others unchanged. Pick `LIGHT`/`DARK` values with ≥ 3:1 contrast of `line`, `up`, `down` against `background`.

- [ ] **Step 4: Wire it.** `main.py`: after `QApplication(...)`, `theme.apply(app, prefs.load(data_dir).get("theme", "system"))`. `MainWindow`: a "View" menu with three checkable actions in an exclusive `QActionGroup`; choosing one calls `theme.apply` and `prefs.save(data_dir, theme=choice)`. `CompareWindow`: `theme.style_plot(self.plot)` at build and on `theme.watcher().changed` (also call `self.redraw()`); pens and header colours drawn through `theme.shade(color)` — `COLORS` stay the identity keys. `paper_ui.py` red/orange stylesheet colours go through `theme.shade`.

- [ ] **Step 5: Add a test** that `CompareWindow` built after `theme.apply(qapp, "dark")` (fallback forced) has a dark plot background, and that switching back to light restyles the open window on `processEvents()`.

- [ ] **Step 6: Run** `uv run pytest -q` — all pass.

- [ ] **Step 7: Commit** `feat: add dark/light display that follows the system`.

### Task 2: Session calendar

**Files:**
- Create: `app/sessions.py`, `app/market.py`, `tests/test_sessions.py`
- Modify: `app/store.py` (tables `sessions`, `meta`), `app/feed.py` (`Poller(trading=None)` refresh step), `app/ui.py` (`start_poller` passes `TradingClient(*keys, paper=True)`)

**Interfaces:**
- Produces:
  - `sessions.Session(day: date, open: int, close: int)` frozen dataclass (Unix seconds).
  - `sessions.fallback(days: Iterable[date]) -> list[Session]` — weekdays given, 09:30–16:00 ET.
  - `sessions.load(store: Store) -> list[Session]` — cached calendar sorted by day; when empty, `fallback` over NY dates that have bars (logs a warning once per process).
  - `sessions.holidays(sessions: list[Session]) -> list[date]` — weekdays between first and last session day that aren't sessions.
  - `sessions.regular(bars: list[Bar], sessions: list[Session]) -> dict[date, list[Bar]]` — regular bars grouped by session day, in time order; days with none are absent.
  - `sessions.to_x(ts: int, holidays: list[date]) -> float | None` and `sessions.from_x(x: float, holidays: list[date]) -> int` (see Step 3).
  - `Store.save_sessions(rows: list[Session]) -> None` (upsert), `Store.sessions() -> list[Session]`, `Store.meta(key: str) -> str | None`, `Store.set_meta(key: str, value: str) -> None`.
  - `market.fetch_sessions(trading, start: date, end: date) -> list[Session]` via `GetCalendarRequest`; `open`/`close` are naive NY datetimes in alpaca-py — localise with `ET`.
  - `market.refresh(trading, store: Store, now: datetime) -> None` — if `meta("calendar_at")` is older than 1 day: fetch `[today - 35, today + 10]`, save, set meta. Catches every exception and logs it.
  - `Poller(..., trading=None)`: calls `market.refresh(self.trading, store, datetime.now(ET))` after each successful backfill when `trading` is set.

- [ ] **Step 1: Write failing tests:**

```python
EARLY = Session(date(2026, 11, 27), ts(2026, 11, 27, 9, 30), ts(2026, 11, 27, 13, 0))

def test_regular_excludes_closing_minute_and_extended_hours():
    day = Session(date(2026, 10, 16), ts(2026, 10, 16, 9, 30), ts(2026, 10, 16, 16, 0))
    bars = [bar("A", ts(2026, 10, 16, h, m)) for h, m in [(8, 0), (9, 30), (15, 59), (16, 0), (19, 0)]]
    assert [b.ts for b in sessions.regular(bars, [day])[day.day]] == [ts(2026, 10, 16, 9, 30), ts(2026, 10, 16, 15, 59)]

def test_regular_respects_early_close():
    bars = [bar("A", ts(2026, 11, 27, 12, 59)), bar("A", ts(2026, 11, 27, 13, 0)), bar("A", ts(2026, 11, 27, 15, 0))]
    assert [b.ts for b in sessions.regular(bars, [EARLY])[EARLY.day]] == [ts(2026, 11, 27, 12, 59)]

def test_x_mapping_skips_weekends_and_holidays_and_round_trips():
    hol = [date(2026, 11, 26)]  # Thanksgiving
    wed, fri = ts(2026, 11, 25, 19, 59), ts(2026, 11, 27, 4, 0)
    assert sessions.to_x(fri, hol) - sessions.to_x(wed, hol) == 1
    assert sessions.to_x(ts(2026, 11, 28, 10, 0), hol) is None   # Saturday
    assert sessions.to_x(ts(2026, 11, 25, 20, 0), hol) is None    # after 20:00
    assert sessions.from_x(sessions.to_x(wed, hol), hol) == wed

def test_market_refresh_failure_is_logged_not_raised(tmp_path, caplog):
    class Rejecting:
        def get_calendar(self, req): raise HTTPError(401)
    market.refresh(Rejecting(), store(tmp_path), datetime(2026, 11, 16, 12, tzinfo=ET))
    assert store(tmp_path).sessions() == [] and "calendar" in caplog.text

def test_poller_calendar_401_does_not_report_bad_key(tmp_path):
    # Poller with a FakeClient that serves bars and a trading client whose get_calendar raises HTTPError(401):
    # on_auth_failed is never called and on_updated is.
```

- [ ] **Step 2: Run** `uv run pytest tests/test_sessions.py -v` — FAIL.

- [ ] **Step 3: Implement.** x mapping, minutes of the extended day 04:00–20:00 ET laid end to end:

```python
ANCHOR, DAY_MIN = date(2000, 1, 3), 960
def to_x(ts, holidays):
    t = datetime.fromtimestamp(ts, ET)
    m = (t.hour - 4) * 60 + t.minute + t.second / 60
    if t.weekday() >= 5 or t.date() in holidays or not 0 <= m < DAY_MIN:
        return None
    return float(np.busday_count(ANCHOR, t.date(), holidays=holidays)) * DAY_MIN + m
```

`from_x` inverts with `divmod(x, DAY_MIN)` and `np.busday_offset(ANCHOR, n, holidays=holidays)`, then `datetime.combine(day, time(4), ET) + timedelta(minutes=m)`. Tables: `sessions(day TEXT PRIMARY KEY, open INTEGER, close INTEGER) WITHOUT ROWID`, `meta(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID`.

- [ ] **Step 4: Run** `uv run pytest -q` — all pass.

- [ ] **Step 5: Commit** `feat: cache the market calendar for session hours`.

### Task 3: Price chart

**Files:**
- Create: `app/chart.py`, `tests/test_chart.py`
- Modify: `app/ui.py` (replace `self.plot`, `Candles`, `draw`, `set_range`, range buttons with a `PriceChart`)

**Interfaces:**
- Consumes: `sessions.to_x/from_x/holidays/load`, `theme.colors/watcher/style_plot`.
- Produces:
  - `PriceChart(QWidget)`: `RANGES = {"1D": None, "5D": 5, "30D": 30}` (moved from `ui.py`); `set_data(symbol: str, title: str, bars: list[Bar], sessions: list[Session], feed_label: str) -> None`; `set_range(name: str) -> None` (clears hold, re-scopes); `reset() -> None` (clears hold); attribute `hold: tuple[int, int] | None`; attribute `vb` (the `ViewBox`).
  - `SessionAxis(pg.AxisItem)` — tick strings via `from_x`; `HH:MM` when the visible span < 1 trading day, else `Mon DD`.

- [ ] **Step 1: Write failing tests** (`bars` = minute bars over 3 sessions, `S` = their sessions):

```python
def test_range_button_resets_a_panned_view(qapp):
    c = PriceChart(); c.set_data("A", "A · Alpha", bars, S, "SIP, 15 min delayed")
    c.vb.setXRange(0, 10); c.vb.sigRangeChangedManually.emit([True, False])
    assert c.hold is not None
    c.set_range("1D")
    assert c.hold is None and c.visible_ts()[1] >= bars[-1].ts

def test_held_view_keeps_its_times_after_new_bar_and_prune(qapp):
    c = PriceChart(); c.set_data("A", "A", bars, S, "")
    c.vb.setXRange(*[to_x(t, []) for t in (bars[10].ts, bars[50].ts)]); c.vb.sigRangeChangedManually.emit([True, False])
    before = c.visible_ts()
    c.set_data("A", "A", bars[5:] + [later_bar], S, "")
    assert c.visible_ts() == before

def test_unheld_view_follows_latest_bar(qapp): ...   # set_data with a later bar moves the right edge to it
def test_double_click_resets(qapp): ...              # c.on_double_click() clears hold
def test_title_axis_labels_and_line_over_candles(qapp):
    c = PriceChart(); c.set_data("AAPL", "AAPL · Apple Inc.", bars, S, "SIP, 15 min delayed")
    assert "AAPL · Apple Inc. · 1D" in c.plot.plotItem.titleLabel.text
    assert c.plot.getAxis("left").labelText == "Price (USD)"
    assert c.line.opts["pen"].width() >= 2 and c.candles.opacity() < 0.5
```

`visible_ts() -> tuple[int, int]` is a small public helper on `PriceChart` returning `from_x` of the current x range.

- [ ] **Step 2: Run** `uv run pytest tests/test_chart.py -v` — FAIL.

- [ ] **Step 3: Implement `app/chart.py`.** Bars whose `to_x` is `None` are not drawn. Close line: `pg.PlotDataItem` with `connect="finite"`, inserting `NaN` where consecutive bars are more than 1 x apart (gaps inside the trading day stay visible). Candles drawn at `to_x(ts)` with opacity ~0.3, `theme.colors().up/down`. `vb.setMouseEnabled(x=True, y=False)`, `vb.enableAutoRange(axis="y")`, `vb.setAutoVisible(y=True)`. `sigRangeChangedManually` → `hold = visible_ts()`. On `set_data`: if `hold`, map both ends with `to_x` (a `None` end snaps to the nearest drawn bar) and `setXRange(..., padding=0)`; else range start = latest session's 04:00 for 1D, or `last_ts - N*86400` for 5D/30D, end = last bar. Double-click: `self.plot.scene().sigMouseClicked` with `ev.double()` → `on_double_click()` → `reset()` + re-scope. Title: `f"{title} · {range} · {feed_label}"` (omit empty parts). Restyle on `theme.watcher().changed`.

- [ ] **Step 4: Wire into `ui.py`.** `MainWindow.draw()` calls `self.chart.set_data(symbol, title, self.store.bars(symbol, since), sessions.load(self.store), label)`, with `since` = start of 30D window (always load 30 days; the range only sets the view). Delete `Candles` and the old range buttons from `ui.py`.

- [ ] **Step 5: Run** `uv run pytest -q` — all pass. Run the app once (`uv run python -m app.main`) and check the chart by eye in both themes.

- [ ] **Step 6: Commit** `feat: clearer minute chart with zoom that snaps back`.

## Phase 2: SIP switch

### Task 4: SIP feed, 15-minute end, 35-day keep

**Files:**
- Modify: `app/feed.py` (`fetch`, `backfill`, `repair_splits`, `Poller.run` prune, `KEEP_DAYS`), `app/ui.py` (`LIVE_SECONDS`), `tests/test_feed.py`

**Interfaces:**
- Produces: `feed.DELAY = timedelta(minutes=15)`, `feed.KEEP_DAYS = 35`. `plan(...)` floor uses `KEEP_DAYS`; `backfill`/`repair_splits` use `end = now - DELAY` everywhere they used `now`; poller prunes `KEEP_DAYS`. `DAYS = 30` remains for gaps/chart. `ui.LIVE_SECONDS = 300 + 900`.

- [ ] **Step 1: Write failing tests** in `tests/test_feed.py`:

```python
def test_fetch_asks_for_sip_ending_15_minutes_ago(tmp_path):
    c = FakeClient(); now = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
    backfill(c, store(tmp_path), ["A"], now=now)
    assert all(r.feed == DataFeed.SIP and utc(r.end) <= now - timedelta(minutes=15) for r in c.requests)

def test_plan_first_run_fetches_35_days(): ...        # floor = now - 35 days
def test_poller_prunes_to_35_days(tmp_path): ...      # update existing prune test
```

Update existing tests that assert 30-day floors or `DataFeed.IEX` to the new values (`test_plan_first_run_fetches_30_days_in_batches_of_10`, `test_plan_never_starts_before_30_day_floor`, `test_poller_prunes_while_running`, `test_fetch_requests_split_adjusted_bars`); rename where the number is in the name.

- [ ] **Step 2: Run** `uv run pytest tests/test_feed.py -v` — new tests FAIL.
- [ ] **Step 3: Implement** the constants and uses above. `FakeClient` already records `requests`; `utc()` in `tests/test_feed.py` normalises alpaca-py's naive UTC times.
- [ ] **Step 4: Run** `uv run pytest -q` — all pass.
- [ ] **Step 5: Commit** `feat: fetch full-market minute bars 15 minutes behind`.

### Task 5: Staged IEX-to-SIP migration

**Files:**
- Modify: `app/store.py`, `app/feed.py`, `app/ui.py`, `tests/test_store.py`, `tests/test_feed.py`

**Interfaces:**
- Produces:
  - `Store`: table `bars_staging` (same columns/PK as `bars`); `staged_latest_all() -> dict[str, int]`; `upsert_staged(bars: Iterable[Bar]) -> None`; `swap_staged() -> None` — one transaction: `DELETE FROM bars`, `INSERT INTO bars SELECT * FROM bars_staging`, `DELETE FROM bars_staging`, `DELETE FROM splits`, `meta feed = "sip"`.
  - `feed.migrate(client, store, symbols, now=None, sleep=time.sleep, progress=..., stop=None) -> bool` — plans from `staged_latest_all()` with `include_empty=True`, fetches each batch (SIP, `end = now - DELAY`) into staging; returns `True` and calls `swap_staged()` only if every batch was fetched without a stop; returns `False` on stop; exceptions propagate.
  - `Poller.run`: while `store.meta("feed") != "sip"`, run `migrate` instead of backfill/split repair; `on_updated` after a successful swap; errors follow today's offline/auth handling (data client 401 is still a real key failure).
  - `MainWindow.feed_label() -> str` — `"SIP, 15 min delayed"` if `meta("feed") == "sip"` else `"IEX, switching to SIP…"`; used in the chart title, the `Last` header tooltip and the status bar.
  - A fresh install (no bars at all) also goes through `migrate`; it simply has nothing to replace.

- [ ] **Step 1: Write failing tests:**

```python
def test_migrate_error_leaves_iex_bars_and_keeps_staged(tmp_path):
    s = store(tmp_path); s.upsert([bar("A", NOW_TS - 3600, c=1.0)])
    c = FailSecond(data=...)  # FakeClient subclass: get_stock_bars raises HTTPError(500) on its 2nd call
    with pytest.raises(HTTPError): migrate(c, s, syms(20), now=NOW)
    assert s.bars("A", 0)[0].c == 1.0 and s.meta("feed") is None and s.staged_latest_all()

def test_migrate_stop_returns_false_and_resume_finishes(tmp_path): ...
    # stop after batch 1 → False, bars untouched; second migrate() fetches only remaining symbols, swaps, feed == "sip"

def test_swap_is_atomic_and_clears_splits(tmp_path): ...
    # make INSERT fail (e.g. monkeypatch db.execute to raise on INSERT) → bars and splits unchanged, meta unchanged

def test_poller_migrates_before_polling(tmp_path): ...
    # feed marker missing → first cycle calls migrate, not backfill; after swap, next cycle backfills
```

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** the interfaces above. `fetch` gains no table parameter; `migrate` calls `store.upsert_staged(fetch(...))`.
- [ ] **Step 4: Run** `uv run pytest -q` — all pass.
- [ ] **Step 5: Commit** `feat: switch stored bars to SIP without losing the old ones first`.

## Phase 3: Names and stats

### Task 6: Regular-session summary

**Files:**
- Modify: `app/store.py` (`summary`), `app/ui.py` (`refresh`), `tests/test_store.py`

**Interfaces:**
- Consumes: `sessions.Session`, `sessions.load`.
- Produces: `Store.summary(sessions: list[Session]) -> dict[str, Summary]` — per symbol: latest session (by day, newest first) that has ≥ 1 regular bar for it → `last` = close of its last regular bar, `volume` = sum of its regular bars' `v`; `prev_close` = close of the last regular bar of the previous session that has any; same one-snapshot `BEGIN`/commit as today. Only probes sessions newest first and stops at the second hit.

- [ ] **Step 1: Rewrite the four summary tests** in `tests/test_store.py` to pass `sessions` and to the regular rule, and add:

```python
def test_summary_ignores_post_market_and_closing_minute(tmp_path):
    s = store(tmp_path); s.upsert([bar("A", ts(2026,10,15,15,59), c=100.0, v=5),
        bar("A", ts(2026,10,16,15,59), c=102.0, v=20), bar("A", ts(2026,10,16,16,0), c=103.0, v=7),
        bar("A", ts(2026,10,16,19,30), c=110.0, v=9)])
    x = s.summary([THU, FRI])["A"]
    assert (x.last, x.prev_close, x.volume) == (102.0, 100.0, 20)

def test_summary_before_open_shows_previous_session(tmp_path):
    # Monday pre-market bar only; Friday and Thursday regular bars → last = Friday close, prev = Thursday close
def test_summary_early_close(tmp_path):
    # EARLY session: bar at 13:30 ignored
def test_summary_no_regular_bars_is_absent(tmp_path):
    # only pre-market bars ever → symbol not in summary (blank row, not 0)
```

`test_session_uses_new_york_date_not_utc` keeps its `session_start` assertion; its summary assertion changes to the regular rule (19:30 bar ignored → `last == 50.0`).

- [ ] **Step 2: Run** `uv run pytest tests/test_store.py -v` — FAIL.
- [ ] **Step 3: Implement**; `MainWindow.refresh` passes `sessions.load(self.store)`.
- [ ] **Step 4: Run** `uv run pytest -q` — all pass.
- [ ] **Step 5: Commit** `feat: list figures use regular-session bars only`.

### Task 7: Viewer listings, names and packaging

**Files:**
- Create: `app/nasdaq100.csv`, `app/dow30.csv`, `app/listing.py`, `tests/test_listing.py`
- Modify: `app/main.py`, `app/ui.py` (Name column), `app/backtest.py` (`selftest`), `packaging/build.sh`, `README.md`

**Interfaces:**
- Produces:
  - `listing.Listing(symbol: str, name: str, sector: str, indexes: frozenset[str], etf_for: str | None)` frozen dataclass. `indexes` ⊆ `{"S&P 500", "Nasdaq-100", "Dow 30"}`; `etf_for` is the group an ETF backs (`"S&P 500"`, `"Nasdaq-100"`, `"Dow 30"`, or a GICS sector name); `sector == "ETF"` for ETFs.
  - `listing.ETFS: dict[str, tuple[str, str]]` — symbol → (name, group). XLK Information Technology, XLF Financials, XLV Health Care, XLY Consumer Discretionary, XLP Consumer Staples, XLE Energy, XLI Industrials, XLB Materials, XLU Utilities, XLRE Real Estate, XLC Communication Services, SPY S&P 500, QQQ Nasdaq-100, DIA Dow 30.
  - `listing.load_listings() -> dict[str, Listing]`; `listing.viewer_symbols() -> list[str]` (sorted, unique).
  - `COLUMNS = ["Symbol", "Name", "Last", "% Chg", "Volume", "Gaps"]` in `ui.py`.

- [ ] **Step 1: Build the CSVs.** Columns `Symbol,Name,GICS Sector`, symbols in the same form as `app/sp500.csv` (e.g. `BRK.B`). Source: Wikipedia "Nasdaq-100" components table (has GICS Sector) and "Dow Jones Industrial Average" components table; for Dow members take Name/Sector from `sp500.csv`. Record source and date in README "Building the app yourself".

- [ ] **Step 2: Write failing tests:**

```python
def test_load_symbols_and_backtest_universe_unchanged():
    assert load_symbols() == [r["Symbol"] for r in csv.DictReader(open("app/sp500.csv"))]
    assert daily.universe() == load_symbols() + ["SPY"]

def test_viewer_symbols_union_without_duplicates():
    v = viewer_symbols()
    assert len(v) == len(set(v)) and set(load_symbols()) <= set(v) and set(ETFS) <= set(v)
    assert {"AAPL"} <= {s for s, l in load_listings().items() if l.indexes == {"S&P 500", "Nasdaq-100", "Dow 30"}}

def test_every_listing_has_name_and_sector():
    assert all(l.name and l.sector for l in load_listings().values())

def test_selftest_loads_viewer_listings(monkeypatch):
    monkeypatch.setattr(listing, "load_listings", lambda: (_ for _ in ()).throw(FileNotFoundError("nasdaq100.csv")))
    assert selftest("packaging/smoke_strategy.py") == 1
```

- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** `main.py` passes `viewer_symbols()` to `MainWindow`; `MainWindow` keeps `listing.load_listings()` for names. `selftest` calls `listing.load_listings()` inside its `try`. `build.sh` adds `--add-data "app/nasdaq100.csv:app" --add-data "app/dow30.csv:app"`.
- [ ] **Step 5: Run** `uv run pytest -q` and `packaging/build.sh && dist/StockBars/StockBars --selftest packaging/smoke_strategy.py` — prints `selftest ok`.
- [ ] **Step 6: Commit** `feat: show company names and track Nasdaq-100, Dow 30 and sector ETFs`.

### Task 8: Stats panel

**Files:**
- Create: `app/quotes.py`, `tests/test_quotes.py`
- Modify: `app/ui.py` (panel under the chart)

**Interfaces:**
- Consumes: `sessions.regular`, `Listing`.
- Produces:
  - `quotes.Quote(name, last, chg_1d, chg_5d, chg_30d, volume, vol, vol_n, sector)` — floats or `None`.
  - `quotes.quote(listing: Listing, bars: list[Bar], sessions: list[Session], today: date) -> Quote` — `bars` = the symbol's stored bars. Closes = last regular bar per session. `last`, `chg_1d`, `volume` agree with `Store.summary`. `chg_Nd` reference = close of the latest session with `day <= today - N days`. Volatility: closes of sessions with `day >= today - 30 days`, consecutive returns, `vol = stdev(ddof=1) * sqrt(252)`, `vol_n` = number of returns; `None` when `vol_n < 2`.
  - `StatsPanel(QTableWidget)` with `show(left: Quote, right: Quote) -> None`; columns = selected symbol, `SPY`; rows Name, Last, Change 1D, Change 5D, Change 30D, Volume, Volatility (30D) shown as `"21.4% (20 d)"`, Sector. `None` → empty cell.

- [ ] **Step 1: Write failing tests:**

```python
def test_changes_use_calendar_lookback_and_blank_when_short():
    q = quote(L, bars_over_40_days, S, today=date(2026, 11, 16))
    assert q.chg_5d == pytest.approx((last / close_on_or_before(date(2026, 11, 11)) - 1) * 100)
    assert quote(L, bars_over_3_days, S3, today=date(2026, 11, 16)).chg_30d is None

def test_volatility_and_sample_size():
    # closes 100, 101, 99, 100 on four sessions → returns n=3, vol = stdev(ddof=1)*sqrt(252)
def test_quote_matches_summary_on_same_bars(tmp_path): ...  # last/chg_1d/volume == Store.summary
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement**; `MainWindow` adds the panel below the chart, updates it in `draw()` (selected symbol + SPY, from `store.bars(symbol, now - KEEP_DAYS*86400)`).
- [ ] **Step 4: Run** `uv run pytest -q` — all pass. **Step 5: Commit** `feat: stats panel comparing a stock with SPY`.

## Phase 4: Grouping

### Task 9: Exchange lookup

**Files:**
- Modify: `app/market.py`, `app/store.py`, `tests/test_sessions.py` (or new `tests/test_market.py`)

**Interfaces:**
- Produces: `market.fetch_exchanges(trading) -> dict[str, str]` via `get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY))`, `{a.symbol: a.exchange.value}`; `Store.save_exchanges(d: dict[str, str]) -> None` (replace all), `Store.exchanges() -> dict[str, str]`. `market.refresh` also refreshes exchanges when `meta("exchanges_at")` is older than 7 days; failure logged, never raised.

- [ ] **Step 1: Write failing tests:** refresh with a fake trading client stores exchanges and sets `exchanges_at`; a second refresh within 7 days does not call `get_all_assets`; a 401 leaves `exchanges()` empty and is logged.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest -q`. **Step 5: Commit** `feat: cache each symbol's exchange`.

### Task 10: Group-by list

**Files:**
- Create: `app/groups.py`, `tests/test_groups.py`
- Modify: `app/ui.py` (`QTableView` → `QTreeView`, group control, click handling), `tests/` UI test for the main window list (new `tests/test_ui_groups.py`)

**Interfaces:**
- Consumes: `Listing`, `ETFS`, `Store.exchanges()`, `Store.summary(...)`, `prefs`.
- Produces:
  - `groups.BY = ("none", "sector", "exchange", "index")`
  - `groups.Group(name: str, members: list[str], avg_pct: float | None, etf: str | None)`.
  - `groups.build(by: str, listings: dict[str, Listing], exchanges: dict[str, str], pct: dict[str, float | None]) -> list[Group]` — sorted by name; ETFs never members; `"index"` puts a stock in every index it belongs to; `"exchange"` uses `"Unknown"` when missing; `avg_pct` = mean of members' non-`None` pct, `None` if none; `etf` from `ETFS` reverse map (exchange groups → `None`). `"none"` → `[]`.
  - UI: a "Group by" `QComboBox` above the list. Flat mode = today's rows (all viewer symbols incl. ETFs). Grouped mode = one top-level row per group (name + `f"({len(members)})"`, avg % Chg coloured like rows) with member rows as children. Clicking a group row with `etf` charts that ETF and fills the panel; without `etf` toggles expansion. Grouping and expanded group names saved via `prefs.save(data_dir, group_by=..., expanded=[...])` and restored at start.

- [ ] **Step 1: Write failing tests:**

```python
def test_index_groups_hold_stock_in_each_index():
    g = {x.name: x for x in build("index", L, {}, {"AAPL": 1.0, "MSFT": 3.0, "QQQ": 9.0})}
    assert "AAPL" in g["Dow 30"].members and "AAPL" in g["Nasdaq-100"].members
    assert "QQQ" not in g["Nasdaq-100"].members and g["Nasdaq-100"].etf == "QQQ"

def test_average_skips_blanks_and_none_when_all_blank():
    g = build("sector", L, {}, {"AAPL": 1.0, "MSFT": None})
    assert next(x for x in g if x.name == "Information Technology").avg_pct == 1.0

def test_unknown_exchange_group(): ...
def test_group_click_charts_etf(qapp, tmp_path): ...       # MainWindow with fakes: click "Information Technology" → chart symbol "XLK"
def test_grouping_remembered(qapp, tmp_path): ...          # set "sector", expand one, rebuild window → same state
```

For the window tests, build `MainWindow` with the poller start stubbed (monkeypatch `MainWindow.start_poller` to a no-op) and a temp `bars.db`.

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** Sorting stays enabled; the proxy sorts children within each parent. Selection reads the symbol from column 0 of the source item, skipping group rows (group rows store their `Group` in `Qt.UserRole`).
- [ ] **Step 4: Run** `uv run pytest -q` — all pass. **Step 5: Commit** `feat: group the list by sector, exchange or index`.

### Task 11: Live check and README

**Files:**
- Create: `tests/test_interface_live.py`
- Modify: `README.md` ("What you'll see", "What to expect")

- [ ] **Step 1: Write the live test** (skipped unless `STOCKBARS_LIVE=1`, same guard as `tests/test_paper_live.py`): SIP minute bars for `["AAPL", "SPY", "XLK"]` from `now - 2 days` to `now - 15 min` are non-empty; `market.fetch_exchanges(TradingClient(*load_keys(), paper=True))` has an exchange for every `viewer_symbols()` entry (list any missing in the failure message); `market.fetch_sessions` for the last 10 days returns ≥ 5 sessions with `open < close`.
- [ ] **Step 2: Run** `STOCKBARS_LIVE=1 uv run pytest tests/test_interface_live.py -v -s` — PASS (VPN off).
- [ ] **Step 3: README:** the chart is SIP, 15 minutes behind; figures are regular-session and minute-derived, so a close can differ from the official closing price by a few cents; Group by; View → theme (checked on Linux only; Windows and macOS not yet looked at); the first launch after updating re-downloads about 35 days of bars.
- [ ] **Step 4: Run** `uv run pytest -q` — all pass. **Step 5: Commit** `docs: describe the new main window`.
- [ ] **Step 6: Owner check:** the owner opens the app and looks at the main window in light and dark before the branch is finished.
