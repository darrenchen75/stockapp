# Backtest Compare Screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An in-app screen that runs backtests in the background, keeps every run on this computer, and compares up to 6 runs on one chart and table, with per-run detail and CSV export.

**Architecture:** Three new modules with no Qt dependency: `app/stats.py` (statistics and chart series from a saved portfolio), `app/runs.py` (saved runs as plain files under the data folder), and a `backtest()` function extracted from `app/backtest.main` so the command and the screen share one run path. The Qt layer is `app/compare_ui.py` (window, dialogs and a worker thread). `app/ui.py` only gains an entry point and two guards.

**Tech Stack:** Python 3.12, PySide6, pyqtgraph, pandas, pytest (Qt tests run with `QT_QPA_PLATFORM=offscreen`; no pytest-qt), PyInstaller, `uv`.

**Spec:** `docs/superpowers/specs/2026-11-08-backtest-compare-screen-design.md`, building on `docs/superpowers/specs/2026-11-06-strategy-backtest-design.md`.

## Global Constraints

- Gate: lifted by the owner on 2026-11-08 (spec, "Gate"). Task 0 is done.
- The screen must not reimplement `load_strategy`, `refresh_daily`, `load_bars`, `run` or `write_results`. The screen and `python -m app.backtest` produce identical `portfolio.csv` and `positions.csv` for the same inputs.
- Statistics are computed from saved results each time they are shown, never stored.
- At most 6 runs ticked at once. SPY is outside the limit.
- Every chart line is scaled to start at $100,000.
- Runs live in `<user_data_dir("stockapp")>/runs`, per computer. Nothing uploads strategy copies.
- No order placement of any kind. README's statement that the app never buys or sells stays true.
- Main window layout and minute-viewer behavior are unchanged. `app/ui.py` and `app/main.py` must not import `app.daily` or `app.backtest` at module level; import `app.compare_ui` lazily inside the action handler.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Never commit `Feedback on Stock Bars v0.1.3 Strategy Shell Requirements.pdf` or anything under `.superpowers/`.

## Review Focus

1. A seventh tick is refused with a message, and the six ticked runs stay ticked. Test in Task 5.
2. A run folder with an unknown or older `format` version, or a damaged `run.json`, is still listed (marked) and never crashes the list. Test in Task 3.
3. Closing the window or pressing Cancel mid-run, during the download, on the last day or while saving, saves nothing and leaves no partial run folder. Test in Task 4.
4. Two runs with capitals of $50,000 and $200,000 plot from the same $100,000 start. Test in Task 1.
5. Re-run after the original strategy file was moved or deleted pre-fills the saved copy instead, with a note. Test in Task 6.

---

### Task 0: Confirm the gate

**Files:** none.

- [ ] **Step 1: Check reconciliation**

Read `.superpowers/sdd/2026-11-06-strategy-backtest-module/progress.md` (or `git log`) for `Task 8: complete`. If Task 8 has not passed, **stop and tell the owner**. The plan may not start.

- [ ] **Step 2: Check whether `Result` changed**

If Task 8 changed `Result`'s columns, update `RESULTS_FORMAT` in Task 2 and the column lists in Tasks 1 and 3 to match before starting, and note it in the ledger.

---

### Task 1: Statistics and chart series

**Files:**
- Create: `app/stats.py`
- Test: `tests/test_stats.py`

**Interfaces:**
- Produces, in `app/stats.py` (pandas only, no Qt, no Alpaca):
  - `BASE = 100_000.0`
  - `growth(portfolio: DataFrame) -> Series`: `value` scaled so the first row is `BASE`, indexed by `date`
  - `drawdown(portfolio: DataFrame) -> Series`: `value / value.cummax() - 1`, indexed by `date`
  - `summary(portfolio: DataFrame, positions: DataFrame) -> dict[str, float | None]` with keys `total_return`, `cagr`, `worst_drop`, `rebalances`, `warnings`. `cagr` is `None` when the first and last dates are the same calendar day, and the UI shows it as "—".
  - `best(column_values: dict[str, dict[str, float | None]]) -> dict[str, str]`: for `total_return` and `cagr` the run id with the highest value, for `worst_drop` the one closest to zero; `None` values never win; no entry for `rebalances` or `warnings`

Formulas (spec, "Same numbers as the command"): total return = final / first − 1; CAGR = (final / first) ^ (365.25 / calendar days) − 1; worst drop = min of drawdown; rebalances = number of distinct `decided_on` among `positions` rows with a non-NaN `fill_price`; warnings = count of non-empty `positions.warning`.

- [ ] **Step 1: Write the failing tests**

```python
def portfolio(values, start="2020-01-01"):
    dates = pd.date_range(start, periods=len(values), freq="365D")  # one row per ~year
    return pd.DataFrame({"date": dates, "value": values})

def test_growth_scales_any_capital_to_100k():
    assert growth(portfolio([50_000, 100_000])).tolist() == [100_000, 200_000]
    assert growth(portfolio([200_000, 100_000])).tolist() == [100_000, 50_000]

def test_drawdown_from_running_high():
    assert drawdown(portfolio([100, 120, 90, 130])).tolist() == pytest.approx([0, 0, -0.25, 0])

def test_summary_formulas():
    pos = pd.DataFrame({"decided_on": ["d0", "d0", "d1", "d2"], "fill_price": [10, 11, float("nan"), 12],
                        "warning": [None, "no bar today: target not filled", None, None]})
    s = summary(portfolio([100, 121], start="2020-01-01"), pos)
    assert s["total_return"] == pytest.approx(0.21)
    assert s["cagr"] == pytest.approx(1.21 ** (365.25 / 365) - 1)
    assert s["worst_drop"] == 0 and s["rebalances"] == 2 and s["warnings"] == 1

def test_single_day_run_has_no_cagr():
    s = summary(portfolio([100]), pd.DataFrame(columns=["decided_on", "fill_price", "warning"]))
    assert s["cagr"] is None and s["total_return"] == 0
    assert best({"a": {**s, "cagr": None}, "b": {**s, "cagr": .1}})["cagr"] == "b"

def test_best_marks_return_growth_and_smallest_drop_only():
    b = best({"a": {"total_return": .5, "cagr": .1, "worst_drop": -.3, "rebalances": 9, "warnings": 0},
              "b": {"total_return": .2, "cagr": .2, "worst_drop": -.1, "rebalances": 1, "warnings": 5}})
    assert b == {"total_return": "a", "cagr": "b", "worst_drop": "b"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_stats.py -v`
Expected: FAIL on the missing `app.stats`.

- [ ] **Step 3: Implement `app/stats.py`**

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_stats.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/stats.py tests/test_stats.py
git commit -m "feat: compute backtest statistics and chart series"
```

---

### Task 2: One run path, with progress and cancel

**Files:**
- Modify: `app/backtest.py` (`run`, `main`)
- Test: `tests/test_backtest.py`

**Interfaces:**
- Consumes: the current `run`, `main`, `refresh_daily`, `load_bars`, `universe`, `default_db_path`, `SOURCE`.
- Produces:
  - In `app/daily.py`: `Cancelled(Exception)`, and `refresh_daily(..., cancelled=None)`, which checks `cancelled()` before each batch and raises `Cancelled` without setting the marker. Rows already replaced stay, consistent per symbol, as an interrupted refresh leaves them today.
  - In `app/backtest.py`: `Cancelled` re-exported from `app.daily`. `app.daily` cannot import `app.backtest`, because `app.backtest` imports `app.daily`.
  - `run(..., now=None, progress=None, cancelled=None) -> Result`: `progress(done: int, total: int)` after each trading day; `cancelled() -> bool` checked before each day AND once after the last day, raising `Cancelled` when True. Defaults leave behavior unchanged.
  - `backtest(strategy, start, end, capital, cost_bps, dividends, client, actions, store, download_progress=None, progress=None, cancelled=None) -> Result`: refresh daily data (passing `cancelled`), load `universe()` bars, apply the SPY-coverage check now in `main` (raise `ValueError` with the same message), then `run`. `main` calls it, so its output and messages stay identical.
  - `buy_spy(history, as_of) -> dict`: returns `{"SPY": 1.0}` (the app's benchmark strategy)
  - `RESULTS_FORMAT = 1`: the version of `Result`'s column layout, saved with each run

- [ ] **Step 1: Write the failing tests**

```python
def test_run_reports_progress_each_day():
    calls = []
    run(all_in("A"), plain, D[0], D[3], progress=lambda d, t: calls.append((d, t)))
    assert calls == [(1, 4), (2, 4), (3, 4), (4, 4)]

def test_run_stops_when_cancelled():
    seen = []
    with pytest.raises(Cancelled):
        run(lambda h, d: seen.append(d) or {"A": 1.0}, plain, D[0], D[3], cancelled=lambda: len(seen) == 2)
    assert len(seen) == 2

def test_cancel_during_the_last_day_still_cancels():
    seen = []
    with pytest.raises(Cancelled):
        run(lambda h, d: seen.append(d) or {"A": 1.0}, plain, D[0], D[3], cancelled=lambda: len(seen) == 4)

def test_refresh_stops_between_batches_when_cancelled(tmp_path):
    # two batches (101 symbols); cancelled() turns True after the first batch's progress call
    with pytest.raises(Cancelled):
        refresh_daily(client, actions, store, symbols_in_two_batches, now=..., cancelled=flag,
                      progress=lambda d, t: flag.set())
    assert store.daily_through() is None and len(client.requests) == 1

def test_backtest_matches_main(tmp_path, command):
    # `command` is the existing main-test fixture with patched clients and db path
    assert main(["tests/fixtures/buy_spy.py", "--start", D[0], "--end", D[3], "--out", str(tmp_path / "cli")]) == 0
    result = backtest(load_strategy("tests/fixtures/buy_spy.py"), D[0], D[3], 100_000.0, 0.0, True,
                      *command.clients, command.store)
    write_results(result, str(tmp_path / "fn"), SOURCE)
    for name in ("portfolio.csv", "positions.csv"):
        assert (tmp_path / "cli" / name).read_bytes() == (tmp_path / "fn" / name).read_bytes()
```

Extend the existing `command` fixture so it exposes the fake `clients` tuple and `store` it patched in.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_backtest.py -v`
Expected: FAIL on the missing `Cancelled`, `backtest` and `progress` parameter.

- [ ] **Step 3: Implement the hooks and extract `backtest`**

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass. The existing `main` tests pass unchanged.

- [ ] **Step 5: Commit**

The refresh test goes in `tests/test_daily.py`, reusing its two-batch fakes.

```bash
git add app/backtest.py app/daily.py tests/test_backtest.py tests/test_daily.py
git commit -m "feat: share one backtest path with progress and cancel hooks"
```

---

### Task 3: Saved runs

**Files:**
- Create: `app/runs.py`
- Test: `tests/test_runs.py`

**Interfaces:**
- Consumes: `Result`, `write_results`, `SOURCE`, `RESULTS_FORMAT` from Task 2.
- Produces, in `app/runs.py` (no Qt):
  - `Settings`, a frozen dataclass: `start: str, end: str, capital: float, cost_bps: float, dividends: bool`
  - `RunRecord`, a frozen dataclass: `id: str, folder: Path, name: str, settings: Settings | None, strategy_path: str, ran_at: str, data_through: str | None, app_version: str, format: int | None, readable: bool`
  - `runs_dir() -> Path`: `<user_data_dir("stockapp", appauthor=False)>/runs`, created on demand
  - `save_run(root: Path, result: Result, settings: Settings, strategy_path: str, strategy_bytes: bytes, data_through: str | None, app_version: str, now: datetime | None = None, cancelled: Callable[[], bool] | None = None) -> RunRecord`. `strategy_bytes` is the exact code that ran, captured before execution by the caller. `save_run` never reads `strategy_path`. `cancelled()` is checked once more just before the staging folder is renamed into place; if it returns True, `save_run` removes the staging folder and raises `Cancelled`.
  - `list_runs(root: Path) -> list[RunRecord]`: newest first
  - `load_result(record: RunRecord) -> Result`
  - `rename_run(record: RunRecord, name: str) -> RunRecord`
  - `delete_run(record: RunRecord) -> None`
  - `export_run(record: RunRecord, out_dir: str) -> None`: copies `portfolio.csv` and `positions.csv`

Layout of one run (my call in the spec, "Format"): `runs/<id>/run.json`, `strategy.py` (byte-for-byte copy), `portfolio.csv`, `positions.csv` (written by `write_results`). `id` is `ran_at` as `YYYYMMDD-HHMMSS` plus a 4-character suffix if taken. `save_run` writes everything into `runs/.<id>.tmp/`, then renames the folder into place, so a failure leaves no listed run. `run.json` holds `name` (default: the strategy file's name), `settings`, `strategy_path`, `ran_at`, `data_through`, `app_version`, `format`.

- [ ] **Step 1: Write the failing tests**

```python
def test_save_and_list_round_trip(tmp_path):
    rec = save_run(tmp_path, result_of(plain), SETTINGS, "/gone/s.py", b"x = 1\n", "2026-11-05", "0.1.4")
    [listed] = list_runs(tmp_path)
    assert listed.name == "s.py" and listed.settings == SETTINGS and listed.readable and listed.format == RESULTS_FORMAT
    assert (listed.folder / "strategy.py").read_bytes() == b"x = 1\n"
    assert load_result(listed).portfolio["value"].tolist() == pytest.approx(result_of(plain).portfolio["value"].tolist())

def test_list_is_newest_first(tmp_path): ...  # two saves with now= one second apart

def test_failed_save_leaves_no_run(tmp_path, monkeypatch):
    monkeypatch.setattr("app.runs.write_results", boom)
    with pytest.raises(OSError):
        save_run(tmp_path, ...)
    assert list_runs(tmp_path) == [] and not any(tmp_path.iterdir())

def test_cancel_at_the_save_boundary_leaves_no_run(tmp_path):
    with pytest.raises(Cancelled):
        save_run(tmp_path, ..., cancelled=lambda: True)
    assert list_runs(tmp_path) == [] and not any(tmp_path.iterdir())

def test_truncated_current_format_csv_is_unreadable(tmp_path):
    rec = save_run(tmp_path, ...)
    (rec.folder / "portfolio.csv").write_text("# note\ndate,val")  # header cut short, no rows
    [listed] = list_runs(tmp_path)
    assert listed.readable is False

def test_older_or_damaged_runs_are_listed_not_fatal(tmp_path):
    good = save_run(tmp_path, ...)
    (tmp_path / "20200101-000000").mkdir()
    (tmp_path / "20200101-000000" / "run.json").write_text("{not json")
    old = tmp_path / "20200102-000000"; shutil.copytree(good.folder, old)
    edit_json(old / "run.json", format=0)
    runs = {r.id: r for r in list_runs(tmp_path)}
    assert runs["20200101-000000"].readable is False and runs["20200101-000000"].settings is None
    assert runs["20200102-000000"].format == 0

def test_rename_delete_export(tmp_path): ...  # rename persists; delete removes the folder; export copies both CSVs byte-for-byte
```

A record is `readable` when, for any `format`: `run.json` parses; both CSVs parse with `comment="#"`; `portfolio.csv` has `date` and `value` columns, at least one row, parseable dates and finite values; and `positions.csv` has `decided_on`, `fill_price` and `warning` columns. Older formats that pass these checks stay readable and are marked "older format" in the list. `list_runs` catches any exception per folder, so one bad run never hides the others.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_runs.py -v`
Expected: FAIL on the missing `app.runs`.

- [ ] **Step 3: Implement `app/runs.py`**

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_runs.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/runs.py tests/test_runs.py
git commit -m "feat: save backtest runs with settings and a strategy copy"
```

---

### Task 4: Background run worker

**Files:**
- Create: `app/compare_ui.py` (worker only in this task)
- Create: `tests/test_compare_ui.py` (with an offscreen `QApplication` fixture: set `QT_QPA_PLATFORM=offscreen` before importing PySide6)
- Test: `tests/test_compare_ui.py`

**Interfaces:**
- Consumes: `backtest`, `load_strategy`, `Cancelled`, `StrategyError` (Task 2); `save_run`, `Settings`, `runs_dir` (Task 3); `Store`, `default_db_path`.
- Produces, in `app/compare_ui.py`:
  - `RunWorker(QObject)` with signals `progressed(str, int, int)` (stage `"download"` or `"run"`, done, total), `finished(object)` (a `RunRecord`), `failed(str)` (one line), `stopped()`
  - `RunWorker.__init__(self, strategy_path: str, settings: Settings, make_clients: Callable[[], tuple], db_path: str, root: Path, app_version: str)`
  - `RunWorker.start()` runs on a `threading.Thread` (the pattern `ui.py` already uses) and emits signals; `RunWorker.cancel()` sets a flag that the `cancelled` hooks read; `RunWorker.busy -> bool` stays True until the thread has exited, whether it finished, failed or stopped

Order inside the thread:
1. Read the strategy file's bytes once.
2. Write them to a temp file under `root` and `load_strategy` that file, so the code that runs is the code saved.
3. Open its own `Store(db_path)`. `sqlite3` connections belong to the thread that made them, as the viewer's `Poller` already does.
4. `backtest(..., cancelled=...)`.
5. `save_run(..., strategy_bytes=those bytes, data_through=store.daily_through(), cancelled=...)`.
6. Close the store and delete the temp file in a `finally`.

Messages: a `StrategyError` or `ValueError` emits its own text; a download failure emits `Could not download daily data: <error>`; `Cancelled` from any step emits `stopped()`. Only success saves.

- [ ] **Step 1: Write the failing tests**

Use the fake clients from the Task 2 `command` fixture via `make_clients`. Wait for signals with a small loop over `QApplication.processEvents()` and a 10-second timeout.

```python
def test_worker_saves_a_successful_run(qapp, tmp_path, fakes): ...  # finished emits a RunRecord; list_runs has one run
def test_worker_result_matches_command(qapp, tmp_path, fakes):
    # same strategy, dates and settings via main(...) and via RunWorker: CSV bytes identical
def test_worker_uses_a_real_sqlite_db_from_its_own_thread(qapp, tmp_path, fakes): ...  # db_path is a real temp file; no ProgrammingError
@pytest.mark.parametrize("when", ["download", "last day", "save"])
def test_cancel_saves_nothing(qapp, tmp_path, fakes, when): ...  # cancel at that point -> stopped; list_runs == []; no folders under root; busy False only after stopped
def test_saved_copy_is_the_code_that_ran(qapp, tmp_path, fakes):
    # strategy file edited on disk after the run starts (from the first "run" progress signal) -> saved strategy.py equals the original bytes
def test_original_deleted_mid_run_still_saves(qapp, tmp_path, fakes): ...
def test_strategy_error_names_the_date_and_saves_nothing(qapp, tmp_path, fakes): ...  # failed text contains D[0]
def test_download_failure_saves_nothing(qapp, tmp_path, fakes): ...  # failed text starts with "Could not download daily data"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_compare_ui.py -v`
Expected: FAIL on the missing `app.compare_ui`.

- [ ] **Step 3: Implement `RunWorker`**

Signals cross from the worker thread to the UI thread through Qt's queued connections, as `MainWindow`'s signals do.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_compare_ui.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/compare_ui.py tests/test_compare_ui.py
git commit -m "feat: run backtests on a worker thread with cancel"
```

---

### Task 5: Compare window: run list, chart, table, SPY

**Files:**
- Modify: `app/compare_ui.py`
- Test: `tests/test_compare_ui.py`

**Interfaces:**
- Consumes: `growth`, `drawdown`, `summary`, `best`, `BASE` (Task 1); `list_runs`, `load_result`, `rename_run`, `delete_run` (Task 3); `run`, `buy_spy`, `load_bars` (Task 2).
- Produces, in `app/compare_ui.py`:
  - `CompareWindow(QWidget)`: `__init__(self, db_path: str, root: Path, make_clients, app_version: str)`. It keeps its own UI-thread `Store(db_path)`, used only for cheap reads (`daily_through`).
  - `CompareWindow.benchmark_ready` signal, emitted when the SPY line arrives
  - `CompareWindow.reload()`: re-reads `list_runs(root)`
  - `CompareWindow.ticked() -> list[str]`: ticked run ids, in list order
  - `CompareWindow.set_mode(mode: str)`: `"growth"` or `"drawdown"`
  - `MAX_TICKED = 6`

Layout follows mockup option 5 (left list, chart, table under it). The run list is a `QListWidget` with checkable items: name, date range, cost. The SPY entry is pinned at the bottom and disabled, with a tooltip hint, when `load_bars` returns no SPY rows. The chart is a `pg.PlotWidget` with a `pg.DateAxisItem`, one line per ticked run, and SPY dashed gray. The table is a `QTableWidget` with one column per ticked run plus SPY, and rows Total return, Yearly growth, Worst drop, Rebalances, Warnings. Best cells are bold, and the SPY column is never bold. The results note from `SOURCE` sits under the table. Line colors: 6 colors differing in lightness as well as hue (builder's choice).

SPY benchmark: `run(buy_spy, load_bars(store, symbols=["SPY"]), earliest_start, latest_end, BASE, 0.0, True)`. It is never computed on the UI thread:
- A tick change starts the computation on a `threading.Thread` with its own `Store(db_path)`.
- Results are cached in memory, keyed by `(daily_through, start, end)`.
- Each request carries a generation number, and a result whose generation is no longer current is dropped.
- Run lines and the table appear immediately. The SPY line and column appear when `benchmark_ready` fires, showing "SPY: loading…" until then.

The legend shows `SPY (data to <daily_through>)`. With no run ticked, the chart shows the hint "Tick a run to compare" and plots nothing.

- [ ] **Step 1: Write the failing tests**

```python
def test_seventh_tick_is_refused(qapp, window_with_runs):  # 7 saved runs
    w = window_with_runs
    for i in range(7):
        w.tick(i)
    assert len(w.ticked()) == 6 and "6" in w.message()

def test_chart_has_one_line_per_ticked_run_plus_spy(qapp, window_with_runs): ...  # SPY line counted after benchmark_ready
def test_tick_change_returns_before_the_benchmark_is_computed(qapp, window_with_runs, monkeypatch):
    # patch app.compare_ui.run so the benchmark call blocks on an Event; tick() returns, run lines are drawn, SPY shows "loading"
def test_stale_benchmark_result_is_dropped(qapp, window_with_runs): ...  # tick A, then A+B; the A-only result arriving last does not replace the A+B one
def test_single_day_run_shows_dash_for_growth(qapp, window_with_one_day_run): ...
def test_lines_start_at_100k_whatever_the_capital(qapp, window_capitals_50k_200k): ...  # first y of each line == 100_000
def test_table_bolds_best_but_never_spy(qapp, window_with_runs): ...
def test_spy_disabled_without_daily_data(qapp, empty_store_window): ...
def test_unreadable_run_is_listed_but_cannot_be_ticked(qapp, window_with_damaged_run): ...
def test_drawdown_mode_plots_drawdown(qapp, window_with_runs): ...  # max y <= 0
```

`tick(i)` and `message()` are small test helpers on `CompareWindow`. `message()` returns the last status text shown.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_compare_ui.py -v`
Expected: FAIL on the missing `CompareWindow`.

- [ ] **Step 3: Implement `CompareWindow`**

Rename and delete go in the run list's context menu. Delete asks with a `QMessageBox` before calling `delete_run`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_compare_ui.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/compare_ui.py tests/test_compare_ui.py
git commit -m "feat: compare saved backtest runs on one chart and table"
```

---

### Task 6: New-run dialog, run detail and export

**Files:**
- Modify: `app/compare_ui.py`
- Test: `tests/test_compare_ui.py`

**Interfaces:**
- Consumes: `RunWorker` (Task 4, built with `db_path`), `CompareWindow` (Task 5), `export_run`, `load_result`, `Settings` (Task 3).
- Produces, in `app/compare_ui.py`:
  - `NewRunDialog(QDialog)`: `__init__(self, parent, settings: Settings | None = None, strategy_path: str = "", fallback_path: str = "")`, with fields from mockup option 1. `values() -> tuple[str, Settings]`. A progress bar, a Cancel button and an error label are driven by a `RunWorker`.
  - `CompareWindow.new_run()` and `CompareWindow.rerun(run_id: str)`: both open the dialog. Re-run pre-fills it from the record. If `strategy_path` no longer exists, it pre-fills the saved `strategy.py` copy instead, and shows "Original file not found; using the copy saved with this run".
  - `CompareWindow.busy -> bool`, and `CompareWindow.cancel_run()`
  - `RunDetail(QDialog)`: `__init__(self, parent, record: RunRecord)`, with Positions and Warnings tabs as in mockup option 4. Positions groups `positions.csv` by `decided_on` with a rebalance picker. The results note (the first line of `portfolio.csv`) is shown under the tabs.
  - Export: a button in the window and in `RunDetail` that asks for a folder and calls `export_run`.

New and Re-run are disabled while `busy`. Defaults for a new run: start 2016-01-04, end = last final date, capital 100,000, cost 0, dividends on.

- [ ] **Step 1: Write the failing tests**

```python
def test_rerun_prefills_settings(qapp, window_with_runs): ...
def test_rerun_uses_saved_copy_when_original_is_gone(qapp, window_with_runs, tmp_path):
    os.remove(original_strategy_path)
    dialog = w.rerun(run_id)
    assert dialog.values()[0].endswith("strategy.py") and "Original file not found" in dialog.note()
def test_new_and_rerun_disabled_while_busy(qapp, busy_window): ...
def test_detail_shows_positions_and_warnings(qapp, record_with_warning): ...  # warning text appears in the Warnings tab
def test_export_copies_both_csvs(qapp, window_with_runs, tmp_path, monkeypatch): ...  # patch QFileDialog.getExistingDirectory
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_compare_ui.py -v`
Expected: FAIL on the missing names.

- [ ] **Step 3: Implement the dialog, detail and export**

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_compare_ui.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/compare_ui.py tests/test_compare_ui.py
git commit -m "feat: add new-run dialog, run detail and export"
```

---

### Task 7: Entry from the main window, and its guards

**Files:**
- Modify: `app/ui.py` (`MainWindow.__init__`, `offer_update`, `closeEvent`)
- Test: `tests/test_compare_ui.py`

**Interfaces:**
- Consumes: `CompareWindow`, `CompareWindow.busy`, `CompareWindow.cancel_run()` (Tasks 5 and 6).
- Produces: a `Backtests…` action in a `Tools` menu on `MainWindow.menuBar()` that lazily imports `app.compare_ui` and opens a single `CompareWindow(self.db_path, runs_dir(), make_clients, VERSION)`, where `make_clients` builds `StockHistoricalDataClient(*self.keys)` and `CorporateActionsClient(*self.keys)`. `MainWindow.compare` holds it, or `None`.

Behavior:
- `offer_update` returns early, and offers again on the next daily check, while `self.compare` is busy.
- `closeEvent` asks "A backtest is running. Stop it and quit?" while busy. Yes cancels and closes; No ignores the event. The existing `updating` guard runs first, unchanged.
- The existing table, chart and range buttons are untouched.

- [ ] **Step 1: Write the failing tests**

```python
def test_viewer_still_does_not_import_backtest_modules():
    text = Path("app/ui.py").read_text()
    assert "import app.compare_ui" not in text.split("def open_backtests")[0]  # lazy import only

def test_close_while_running_asks_and_cancels(qapp, main_window_with_busy_compare, monkeypatch): ...  # patch QMessageBox to Yes -> cancel_run called, event accepted
def test_update_offer_waits_while_running(qapp, main_window_with_busy_compare): ...  # offer_update shows no dialog
```

Build `MainWindow` with a temp db, an empty symbol list and patched `start_poller` and `check_for_update`, so no network is touched.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_compare_ui.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the menu action and guards**

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass, and the existing `test_viewer_does_not_import_the_backtest` still passes.

- [ ] **Step 5: Commit**

```bash
git add app/ui.py tests/test_compare_ui.py
git commit -m "feat: open backtests from the main window"
```

---

### Task 8: Packaging and README

**Files:**
- Create: `packaging/hooks/hook-app.compare_ui.py`
- Modify: `.github/workflows/release.yml:43-44`, `StockBars.spec`, `README.md`

- [ ] **Step 1: Add the hook**

Set `hiddenimports` to `sys.stdlib_module_names` minus `tkinter`, `turtle`, `idlelib`, `test`, `lib2to3`, `ensurepip` and `venv`, filtered to modules that import on the build machine, plus `collect_submodules("pandas")` and `collect_submodules("numpy")`.

- [ ] **Step 2: Wire it in**

Add `--additional-hooks-dir packaging/hooks` to the PyInstaller command in `release.yml`, and `hookspath=['packaging/hooks']` in `StockBars.spec`.

- [ ] **Step 3: Build locally and smoke-test**

Run: `uv run pyinstaller --noconfirm StockBars.spec`
Expected: the build succeeds. Run the built app with a strategy file that does `import pandas, numpy, statistics, json` and returns `{"SPY": 1.0}`: it saves a run.

- [ ] **Step 4: Update `README.md`**

Describe the Backtests screen: what it does, that runs stay on this computer, and the results note. Keep the sentence that the app never buys or sells.

- [ ] **Step 5: Commit**

```bash
git add packaging/hooks/hook-app.compare_ui.py .github/workflows/release.yml StockBars.spec README.md
git commit -m "build: bundle the standard library, pandas and numpy for strategies"
```

- [ ] **Step 6: Owner check on each OS (manual)**

After the next release build, the owner runs the Step 3 strategy in the built app on macOS, Windows and Linux. Record the result in the ledger. This step needs the owner and the CI artifacts.
