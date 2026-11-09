# Daily Data and Backtest Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Download years of daily bars and run a strategy file over them from the command line or a notebook, with results proven against a hand-computed case and, finally, the reviewer's case.

**Architecture:** `app/daily.py` downloads raw bars and adjustment factors into a new table in `bars.db`. `app/backtest.py` is a data-agnostic runner: it takes bars as a DataFrame, shows the strategy history adjusted as of each day, and rebalances under self-financing cash rules. The viewer never imports either module, so its behavior and packaging do not change. The app screen is a separate later plan.

**Tech Stack:** Python 3.12, SQLite, pandas, alpaca-py, pytest, `uv`.

**Spec:** `docs/superpowers/specs/2026-11-06-strategy-backtest-design.md`

## Global Constraints

- No broker orders of any kind, paper or live.
- Viewer unchanged: startup, minute data (IEX) and update flow. `app/ui.py` and `app/main.py` must not import `app.daily` or `app.backtest`.
- No real strategy in the repo. Only the dummy in `tests/fixtures/`.
- Daily data: SIP feed, from 2016-01-01; never request bars newer than 15 minutes.
- Decision time: 20:30 New York time on day T. Fill at T+1's open.
- A strategy never sees a day's bar before that day's cutoff, or adjustment from events after the decision day.
- Cash never goes negative.
- Tolerance: 1e-6 on daily returns, only when both sides use identical prices.
- Results carry a visible note of survivorship bias, feed and dividend setting.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Work on branch `feat/strategy-backtest`. Never commit `Feedback on Stock Bars v0.1.3 Strategy Shell Requirements.pdf`.

## Review Focus

1. A stock with no bars yet on a date (ABNB in 2016): the strategy sees blanks, a target for it stays in cash with a warning, and the run does not crash. Test in Task 5.
2. A symbol Alpaca returns nothing for: the refresh finishes, keeps any old rows and logs the symbol. Test in Task 3.
3. A download interrupted partway: each symbol stays internally consistent, and the next run downloads again. Test in Task 3.
4. A date range with no trading days, or start after end: a clear error, not an empty result. Test in Task 5.
5. The viewer running against the same `bars.db`: no "database is locked" failure. Test in Task 2.

---

### Task 1: Verify the data source with a real key (gate)

Uses the owner's stored Alpaca key, read-only. The script is throwaway, so keep it in the session scratchpad and do not commit it. Never print the key.

**Files:**
- Modify: `docs/superpowers/specs/2026-11-06-strategy-backtest-design.md` (record findings under "Verify first")

**Interfaces:**
- Produces: confirmed facts for Tasks 2 and 3, namely the shape of daily timestamps and factor quality.

- [ ] **Step 1: Run a script requesting daily bars**

Use `StockHistoricalDataClient(*settings.load_keys())`, `TimeFrame.Day`, `DataFeed.SIP`, start 2016-01-01, end 16 minutes ago, once each with `Adjustment.RAW`, `SPLIT` and `ALL`, for AAPL, F and SPY.

Expected: all three succeed. If SIP is refused, **stop and return to the owner**, because the data source decision reopens.

- [ ] **Step 2: Print these facts**

- Earliest bar date, and bar count per symbol.
- A sample `timestamp`, confirming it converts to the right date in New York time.
- `adj/raw` close for AAPL across 2020-08-28 and 2020-08-31 (4:1 split). The ratio should be about 4.
- Factor noise: for AAPL 2024-08-13 through 2024-11-07 (no dividend inside), and for F over a similar stretch between its dividends, the largest day-to-day relative change in `ALL` close divided by `RAW` close.
- Whether the date sets match across RAW, SPLIT and ALL for each symbol. A mismatch here would make the Task 3 refresh fail, so report it to the owner before continuing.

- [ ] **Step 3: Apply the factor gate**

If noise is at most 1e-6, continue. If larger, **stop and show the owner the numbers.** The spec's fallback is building factors from corporate actions data, which changes Task 3.

- [ ] **Step 4: Record the findings and commit**

Add two or three lines of findings under "Verify first" in the spec, with the date.

```bash
git add docs/superpowers/specs/2026-11-06-strategy-backtest-design.md
git commit -m "docs: record Alpaca daily data findings"
```

---

### Task 2: Daily table in the store

**Files:**
- Modify: `app/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces, in `app/store.py`:
  - `DailyBar`, a frozen dataclass: `symbol: str, date: str` (`YYYY-MM-DD`, New York date), `o, h, l, c: float` (raw), `v: float` (raw), `factor_all: float, factor_split: float`
  - `Store.replace_daily(symbol: str, bars: Iterable[DailyBar]) -> None`: delete and insert in one transaction
  - `Store.daily_rows(through: str) -> Iterable[tuple]`: rows in `DailyBar` field order with `date <= through`, ordered by date, symbol
  - `Store.daily_through() -> str | None` and `Store.set_daily_through(date: str) -> None`: the date of the last completed refresh

- [ ] **Step 1: Write the failing tests**

```python
def daily(symbol, date, c=10.0, f=1.0):
    return DailyBar(symbol, date, c, c, c, c, 100.0, f, f)

def test_replace_daily_swaps_all_rows_of_one_symbol(tmp_path):
    s = store(tmp_path)
    s.replace_daily("A", [daily("A", "2020-01-02"), daily("A", "2020-01-03")])
    s.replace_daily("B", [daily("B", "2020-01-02")])
    s.replace_daily("A", [daily("A", "2020-01-03", f=0.5)])
    rows = list(s.daily_rows("2099-01-01"))
    assert [(r[0], r[1], r[7]) for r in rows] == [("B", "2020-01-02", 1.0), ("A", "2020-01-03", 0.5)]

def test_daily_rows_stop_at_through(tmp_path):
    s = store(tmp_path)
    s.replace_daily("A", [daily("A", "2020-01-02"), daily("A", "2020-01-03")])
    assert [r[1] for r in s.daily_rows("2020-01-02")] == ["2020-01-02"]

def test_failed_replace_keeps_old_rows(tmp_path):
    s = store(tmp_path)
    s.replace_daily("A", [daily("A", "2020-01-02")])
    with pytest.raises(sqlite3.Error):
        s.replace_daily("A", [daily("A", "2020-01-03"), daily("A", "2020-01-03")])
    assert [r[1] for r in s.daily_rows("2099-01-01")] == ["2020-01-02"]

def test_prune_leaves_daily_rows(tmp_path):
    s = store(tmp_path)
    s.replace_daily("A", [daily("A", "2016-01-04")])
    s.prune(30)
    assert len(list(s.daily_rows("2099-01-01"))) == 1

def test_daily_through_round_trips(tmp_path):
    s = store(tmp_path)
    assert s.daily_through() is None
    s.set_daily_through("2026-11-05")
    assert s.daily_through() == "2026-11-05"

def test_daily_write_while_another_connection_reads(tmp_path):
    a, b = store(tmp_path), store(tmp_path)
    a.replace_daily("A", [daily("A", "2020-01-02")])
    b.db.execute("BEGIN")
    b.db.execute("SELECT * FROM bars").fetchall()
    a.replace_daily("A", [daily("A", "2020-01-03")])  # must not raise "database is locked"
    b.db.commit()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL on the missing `DailyBar` import.

- [ ] **Step 3: Implement in `app/store.py`**

Add tables `daily_bars` (primary key `symbol, date`, `WITHOUT ROWID`) and `daily_meta` with `CREATE TABLE IF NOT EXISTS` in `Store.__init__`, following the `splits` table. Use plain `INSERT` in `replace_daily`, not `INSERT OR REPLACE`, so bad data raises. `prune` stays untouched.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/store.py tests/test_store.py
git commit -m "feat: store daily bars with adjustment factors"
```

---

### Task 3: Download daily data and apply the cutoff

**Files:**
- Create: `app/daily.py`
- Modify: `app/feed.py:62-81` (extract the retry loop), `pyproject.toml` and `uv.lock` (declare pandas)
- Test: `tests/test_daily.py`

**Interfaces:**
- Consumes: `DailyBar` and the `Store` daily methods from Task 2; `feed.MAX_SYMBOLS`, `feed.load_symbols`.
- Produces, in `app/feed.py`: `with_retries(call: Callable[[], T], sleep=time.sleep) -> T`, the existing 429 loop. `fetch` uses it, and `tests/test_feed.py` stays unchanged.
- Produces, in `app/daily.py`:
  - `HISTORY_START = date(2016, 1, 1)`
  - `decision_time(day: date) -> datetime`: 20:30 `ET` on `day`
  - `last_final_date(now: datetime) -> date`: latest date whose decision time is at or before `now`
  - `universe() -> list[str]`: `load_symbols() + ["SPY"]`
  - `default_db_path() -> str`: `bars.db` in `user_data_dir("stockapp", appauthor=False)`, creating the directory
  - `IncompleteDownload(Exception)`: its message names the affected symbols
  - `fetch_daily(client, symbols, through: date, sleep=time.sleep) -> tuple[dict[str, list[DailyBar]], list[str]]`: complete symbols, then symbols whose RAW, SPLIT and ALL date sets differ
  - `refresh_daily(client, store, symbols, now=None, sleep=time.sleep, progress=lambda done, total: None) -> bool`: True if it downloaded, False if already current. Raises `IncompleteDownload` after finishing the other symbols if any symbol's dates differ.
  - `load_bars(store, now=None) -> pandas.DataFrame`: columns `symbol, date, o, h, l, c, v, factor_all, factor_split`, with `date` as `datetime64` and only dates up to `last_final_date(now)`

- [ ] **Step 1: Write the failing tests**

Build a `FakeClient` as in `tests/test_feed.py` whose `get_stock_bars` looks up bars by `req.adjustment`, and records requests. Compare datetimes with the `utc` helper from `tests/test_feed.py`, since alpaca-py may normalize them.

```python
NOV5 = date(2026, 11, 5)  # a Thursday

def test_last_final_date_turns_at_2030_new_york():
    assert last_final_date(datetime(2026, 11, 5, 20, 29, tzinfo=ET)) == date(2026, 11, 4)
    assert last_final_date(datetime(2026, 11, 5, 20, 30, tzinfo=ET)) == NOV5

def test_fetch_daily_requests_sip_day_bars_three_ways():
    client = make_client(...)
    fetch_daily(client, ["A"], NOV5)
    assert {r.adjustment for r in client.requests} == {Adjustment.RAW, Adjustment.SPLIT, Adjustment.ALL}
    assert all(r.feed == DataFeed.SIP and r.timeframe.value == TimeFrame.Day.value for r in client.requests)
    assert all(utc(r.start) == datetime(2016, 1, 1, tzinfo=ET).astimezone(timezone.utc) for r in client.requests)
    # 20:15 on `through`: at the 20:30 cutoff this is already 15 minutes old
    assert all(utc(r.end) == datetime(2026, 11, 5, 20, 15, tzinfo=ET).astimezone(timezone.utc) for r in client.requests)

def test_fetch_daily_batches_at_most_100_symbols():
    client = make_client(...)
    fetch_daily(client, [f"S{i}" for i in range(101)], NOV5)
    assert sorted(len(r.symbol_or_symbols) for r in client.requests) == [1, 1, 1, 100, 100, 100]

def test_fetch_daily_derives_factors_from_closes():
    # raw close 100, split-adjusted 25, fully adjusted 24
    bars = fetch_daily(make_client(...), ["A"], NOV5)[0]["A"]
    assert bars[0].c == 100 and bars[0].factor_split == 0.25 and bars[0].factor_all == 0.24

def test_fetch_daily_uses_new_york_date():
    # timestamp 2020-01-02 05:00 UTC is midnight New York on 2020-01-02
    assert fetch_daily(make_client(...), ["A"], NOV5)[0]["A"][0].date == "2020-01-02"

def test_mismatched_dates_keep_old_history_and_other_symbols_finish(tmp_path):
    # For A, ALL lacks one of RAW's two dates; B is complete
    store.replace_daily("A", [old_row])
    with pytest.raises(IncompleteDownload, match="A"):
        refresh_daily(partial_client, store, ["A", "B"], now=...)
    assert [r for r in store.daily_rows("2099-01-01") if r[0] == "A"] == [old_row_tuple]
    assert "B" in {r[0] for r in store.daily_rows("2099-01-01")}
    assert store.daily_through() is None

def test_healthy_retry_replaces_the_whole_snapshot_after_a_mismatch(tmp_path):
    with pytest.raises(IncompleteDownload):
        refresh_daily(partial_client, store, ["A"], now=...)
    assert refresh_daily(healthy_client, store, ["A"], now=...) is True
    assert {r[1] for r in store.daily_rows("2099-01-01")} == {"2020-01-02", "2020-01-03"}
    assert store.daily_through() == "2026-11-05"

def test_refresh_is_skipped_when_current(tmp_path):
    store.set_daily_through("2026-11-05")
    assert refresh_daily(client, store, ["A"], now=datetime(2026, 11, 5, 21, 0, tzinfo=ET)) is False
    assert client.requests == []

def test_refresh_sets_marker_only_when_finished(tmp_path):
    assert refresh_daily(client, store, ["A"], now=datetime(2026, 11, 5, 21, 0, tzinfo=ET)) is True
    assert store.daily_through() == "2026-11-05"

def test_interrupted_refresh_leaves_marker_unset_and_symbols_consistent(tmp_path):
    # 2 batches; the client raises a non-429 error on the second batch's first request
    with pytest.raises(HTTPError):
        refresh_daily(client, store, symbols_in_two_batches, now=...)
    assert store.daily_through() is None
    # first batch's symbols have rows; second batch's old rows are untouched
    assert refresh_daily(healthy_client, store, symbols_in_two_batches, now=...) is True

def test_symbol_without_bars_keeps_old_rows_and_finishes(tmp_path):
    store.replace_daily("GONE", [old_row])
    assert refresh_daily(client_without_gone, store, ["A", "GONE"], now=...) is True
    assert "GONE" in {r[0] for r in store.daily_rows("2099-01-01")}

def test_load_bars_hides_a_day_before_its_cutoff(tmp_path):
    store.replace_daily("A", [row("2026-11-04"), row("2026-11-05")])
    before = load_bars(store, now=datetime(2026, 11, 5, 20, 29, tzinfo=ET))
    after = load_bars(store, now=datetime(2026, 11, 5, 20, 30, tzinfo=ET))
    assert before["date"].max() == pd.Timestamp("2026-11-04")
    assert after["date"].max() == pd.Timestamp("2026-11-05")

def test_universe_adds_spy():
    assert universe()[-1] == "SPY" and "AAPL" in universe()
```

Fill the `...` placeholders with fake bars local to the test file.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_daily.py -v`
Expected: FAIL on the missing `app.daily`.

- [ ] **Step 3: Declare pandas**

Run: `uv add pandas`
Expected: `pyproject.toml` lists pandas, and the locked major version stays 3.x.

- [ ] **Step 4: Extract `with_retries` in `app/feed.py`, and implement `app/daily.py`**

The request `end` is `decision_time(through) - timedelta(minutes=15)`. `refresh_daily` computes `through = last_final_date(now)`, and each symbol gets one `replace_daily` call, so symbols stay internally consistent. Symbols with no bars in all three responses skip `replace_daily` and are logged.

A symbol whose date sets differ across RAW, SPLIT and ALL keeps its old rows untouched. `refresh_daily` finishes the other symbols, then raises `IncompleteDownload` without calling `set_daily_through`, so the next run downloads again and a backtest cannot run on a partial refresh. `set_daily_through` runs last, and only when no symbol was incomplete. If the mismatch persists for a symbol, the command keeps failing with its name; return to the owner rather than dropping dates.

`app/daily.py` must not import `app.settings` or anything from Qt.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass, including `tests/test_feed.py` unchanged.

- [ ] **Step 6: Commit**

```bash
git add app/daily.py app/feed.py pyproject.toml uv.lock tests/test_daily.py
git commit -m "feat: download daily bars with as-of adjustment factors"
```

---

### Task 4: Prices as of each day

**Files:**
- Create: `app/backtest.py`
- Test: `tests/test_backtest.py`

**Interfaces:**
- Consumes: only the DataFrame shape from `load_bars`, with `factor_all` and `factor_split` optional (default 1.0) so a test case can supply plain prices. `date` may be strings or datetimes.
- Produces, in `app/backtest.py`:
  - `Prices`, a frozen dataclass: `raw: dict[str, DataFrame]` for `o, h, l, c, v` (dates by symbols, NaN where no bar), `factor: DataFrame`, `split: DataFrame` (both forward-filled per symbol), `days: list[Timestamp]`
  - `prepare(bars: DataFrame, dividends: bool = True) -> Prices`: `factor` is `factor_all` if `dividends`, else `factor_split`. `days` are SPY's dates if SPY is present, else all dates.
  - `history_as_of(prices: Prices, day: Timestamp) -> dict[str, DataFrame]`: keys `o, h, l, c, v`, rows through `day`, fresh copies

- [ ] **Step 1: Write the failing tests**

```python
def bars(rows):
    """rows: (symbol, date, o, c, v, factor_all, factor_split); h and l equal the larger and smaller of o and c."""

D = ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"]
SPLIT = bars([("A", D[0], 100, 100, 10, .5, .5), ("A", D[1], 100, 100, 10, .5, .5),
              ("A", D[2], 50, 50, 20, 1, 1), ("A", D[3], 50, 60, 20, 1, 1)])
DIVIDEND = bars([("A", D[0], 100, 100, 10, .98, 1), ("A", D[1], 100, 100, 10, .98, 1),
                 ("A", D[2], 98, 98, 10, 1, 1)])

def test_history_before_a_split_shows_prices_as_they_traded():
    h = history_as_of(prepare(SPLIT), pd.Timestamp(D[1]))
    assert h["c"]["A"].tolist() == [100, 100]
    assert h["v"]["A"].tolist() == [10, 10]

def test_history_after_a_split_restates_earlier_days():
    h = history_as_of(prepare(SPLIT), pd.Timestamp(D[2]))
    assert h["c"]["A"].tolist() == [50, 50, 50]
    assert h["v"]["A"].tolist() == [20, 20, 20]

def test_dividend_restates_price_but_not_volume():
    h = history_as_of(prepare(DIVIDEND), pd.Timestamp(D[2]))
    assert h["c"]["A"].tolist() == [98, 98, 98]
    assert h["v"]["A"].tolist() == [10, 10, 10]

def test_splits_only_setting_ignores_dividends():
    h = history_as_of(prepare(DIVIDEND, dividends=False), pd.Timestamp(D[2]))
    assert h["c"]["A"].tolist() == [100, 100, 98]

def test_history_uses_last_factor_when_a_symbol_has_no_bar_that_day():
    # A has bars on D[0] and D[1] only (factors .5), B has all three days
    h = history_as_of(prepare(two_symbols), pd.Timestamp(D[2]))
    assert h["c"]["A"].tolist()[:2] == [100, 100] and pd.isna(h["c"]["A"].iloc[2])

def test_days_come_from_spy_when_present():
    # SPY has D[0] and D[1]; A also has D[2]
    assert prepare(with_spy).days == [pd.Timestamp(D[0]), pd.Timestamp(D[1])]

def test_changing_returned_history_does_not_change_prices():
    p = prepare(SPLIT)
    history_as_of(p, pd.Timestamp(D[1]))["c"].iloc[0, 0] = -1
    assert history_as_of(p, pd.Timestamp(D[1]))["c"]["A"].iloc[0] == 100
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_backtest.py -v`
Expected: FAIL on the missing `app.backtest`.

- [ ] **Step 3: Implement `prepare` and `history_as_of`**

Prices: `raw(t) * factor(t) / factor(day)`. Volume: `raw_v(t) * split(day) / split(t)`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_backtest.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/backtest.py tests/test_backtest.py
git commit -m "feat: show strategies prices adjusted as of each day"
```

---

### Task 5: Cash rules and the runner

**Files:**
- Modify: `app/backtest.py`
- Test: `tests/test_backtest.py`, `tests/test_reconciliation.py`

**Interfaces:**
- Consumes: `Prices`, `prepare`, `history_as_of` from Task 4.
- Produces, in `app/backtest.py`:
  - `StrategyError(Exception)`: its message includes the decision date
  - `validate_weights(weights, symbols, day) -> dict[str, float]`
  - `Fill`, a frozen dataclass: `shares: dict[str, float], cash: float, scaled: bool, unfilled: list[str]`
  - `rebalance(shares, cash, targets, open_px, last_px, cost_bps=0.0) -> Fill`: all prices adjusted. `open_px` holds only symbols with a bar at this open. `last_px` is each symbol's last known close.
  - `Result`, a frozen dataclass: `portfolio: DataFrame` (`date, value, return, cash, warning`), `positions: DataFrame` (`decided_on, date, symbol, target_weight, fill_price, shares, warning`), `dividends: bool`
  - `run(strategy, bars, start, end, capital=100_000.0, cost_bps=0.0, dividends=True) -> Result`

- [ ] **Step 1: Write the failing `rebalance` tests**

```python
def test_sales_pay_for_purchases():
    f = rebalance({"A": 10}, 0, {"B": 1}, {"A": 10, "B": 5}, {"A": 10, "B": 5})
    assert f.shares["B"] == 20 and f.shares.get("A", 0) == 0 and f.cash == 0 and not f.scaled

def test_blocked_sale_cannot_fund_a_purchase():
    f = rebalance({"A": 10}, 0, {"B": 1}, {"B": 5}, {"A": 10, "B": 5})
    assert f.shares["A"] == 10 and f.shares.get("B", 0) == 0 and f.cash == 0 and f.scaled

def test_blocked_sale_limits_purchase_to_available_cash():
    f = rebalance({"A": 10}, 100, {"B": 1}, {"B": 5}, {"A": 10, "B": 5})
    assert f.shares["A"] == 10 and f.shares["B"] == 20 and f.cash == 0 and f.scaled

def test_full_target_with_costs_scales_to_zero_cash():
    f = rebalance({}, 1000, {"A": 1}, {"A": 10}, {}, cost_bps=100)
    assert f.shares["A"] == pytest.approx(1000 / 1.01 / 10) and f.cash == pytest.approx(0) and f.cash >= 0 and f.scaled

def test_sale_pays_its_cost():
    f = rebalance({"A": 10}, 0, {}, {"A": 10}, {"A": 10}, cost_bps=100)
    assert f.cash == pytest.approx(99) and f.shares.get("A", 0) == 0

def test_zero_cost_full_target_needs_no_scaling():
    f = rebalance({"A": 10}, 0, {"A": .5, "B": .5}, {"A": 10, "B": 5}, {"A": 10, "B": 5})
    assert f.shares["A"] == pytest.approx(5) and f.shares["B"] == pytest.approx(10)
    assert f.cash == pytest.approx(0) and f.cash >= 0 and not f.scaled

def test_target_without_an_open_stays_in_cash():
    f = rebalance({}, 100, {"B": 1}, {}, {})
    assert f.unfilled == ["B"] and f.cash == 100 and f.shares.get("B", 0) == 0
```

- [ ] **Step 2: Write the failing `validate_weights` tests**

Parametrize over each bad case and assert `pytest.raises(StrategyError, match="2020-01-02")`:
an unknown symbol, weight -0.1, weight 1.1, weights totalling 1.5, NaN, and a non-dict return such as `None`.

Also assert that `{}` returns `{}` (all cash), that numpy floats are accepted, and that a total within 1e-9 above 1 is accepted.

- [ ] **Step 3: Write the failing `run` tests in `tests/test_reconciliation.py`**

These hand-computed cases are the synthetic reconciliation. Use `all_in("A")`, a small helper returning `lambda history, as_of: {"A": 1.0}`.

```python
def test_full_run_matches_hand_computed_values():
    # A: d1 o10 c10; d2 o10 c11; d3 o12 c12; d4 o12 c15
    r = run(all_in("A"), plain, D[0], D[3], capital=1000)
    assert r.portfolio["value"].tolist() == pytest.approx([1000, 1100, 1200, 1500], abs=1e-6)
    assert r.portfolio["return"].tolist() == pytest.approx([0, .1, 1 / 11, .25], abs=1e-6)

def test_fills_at_next_open_and_records_final_decision_with_no_fill():
    r = run(all_in("A"), plain, D[0], D[3], capital=1000)
    assert r.positions.iloc[0]["fill_price"] == 10 and r.positions.iloc[0]["shares"] == 100
    last = r.positions.iloc[-1]
    assert last["decided_on"] == pd.Timestamp(D[3]) and pd.isna(last["date"]) and last["target_weight"] == 1

def test_split_does_not_change_value_and_shares_report_actual_count():
    # SPLIT from Task 4, with d4 close 60
    r = run(all_in("A"), SPLIT, D[0], D[3], capital=1000)
    assert r.portfolio["value"].tolist() == pytest.approx([1000, 1000, 1000, 1200], abs=1e-6)
    assert r.positions["shares"].tolist()[:2] == pytest.approx([10, 20])
    assert r.positions["fill_price"].iloc[0] == 100

def test_dividend_counts_only_when_setting_is_on():
    on = run(all_in("A"), DIVIDEND, D[0], D[2], capital=1000)
    off = run(all_in("A"), DIVIDEND, D[0], D[2], capital=1000, dividends=False)
    assert on.portfolio["value"].tolist() == pytest.approx([1000, 1000, 1000], abs=1e-6)
    assert off.portfolio["value"].tolist() == pytest.approx([1000, 1000, 980], abs=1e-6)

def test_later_prices_and_splits_do_not_change_earlier_decisions():
    # Strategy records every history it receives. Second run: from D[3] prices halve
    # and earlier factors halve, as a later split would do.
    seen_a, seen_b = record(base), record(with_later_split)
    for day in D[:3]:
        for field in "ohlcv":
            pd.testing.assert_frame_equal(seen_a[day][field], seen_b[day][field])

def test_strategy_never_receives_bars_after_its_decision_day():
    # Strategy asserts history["c"].index.max() == as_of for every call

def test_blocked_sale_then_replacement_keeps_cash_non_negative():
    # Hold A, then switch to 100% B on a day A has no next bar
    r = run(switch_to_b, bars_with_a_gap, D[0], D[3], capital=1000)
    assert (r.portfolio["cash"] >= 0).all() and r.portfolio["warning"].notna().any()

def test_full_target_with_costs_keeps_cash_non_negative():
    r = run(all_in("A"), plain, D[0], D[3], capital=1000, cost_bps=100)
    assert (r.portfolio["cash"] >= 0).all()
    assert r.portfolio["value"].iloc[1] == pytest.approx(1000 / 1.01 / 10 * 11, abs=1e-6)

def test_symbol_not_yet_trading_stays_in_cash_with_warning():
    # B's first bar is D[2]; strategy targets 100% B from D[0]
    r = run(all_in("B"), late_listing, D[0], D[3], capital=1000)
    assert r.portfolio["value"].iloc[1] == 1000
    assert r.positions["warning"].notna().any()

def test_held_symbol_with_no_bar_keeps_last_price_with_warning():
    r = run(all_in("A"), bars_with_a_gap, D[0], D[3], capital=1000)
    assert r.positions["warning"].notna().any()

def test_strategy_exception_names_the_date():
    with pytest.raises(StrategyError, match=D[0]):
        run(raises, plain, D[0], D[3])

@pytest.mark.parametrize("start,end", [("2030-01-01", "2030-02-01"), (D[3], D[0])])
def test_range_without_trading_days_is_a_clear_error(start, end):
    with pytest.raises(ValueError, match="no trading days"):
        run(all_in("A"), plain, start, end)
```

Fill in the named fixtures (`plain`, `bars_with_a_gap`, `late_listing`, `with_later_split`, `base`) local to the test file.

- [ ] **Step 4: Run them to verify they fail**

Run: `uv run pytest tests/test_backtest.py tests/test_reconciliation.py -v`
Expected: FAIL on the missing names.

- [ ] **Step 5: Implement `validate_weights` and `rebalance`**

Follow the spec's cash rules. Cost is `cost_bps / 10000` of traded value. When purchases plus costs exceed cash, scale by `cash / (total_purchases * (1 + cost))`. `scaled` is True only if the shortfall exceeds `1e-9` of portfolio value, though smaller rounding is still scaled away. Clamp final cash to at least 0 only within that same tolerance.

- [ ] **Step 6: Implement `run`**

Internally, shares are in adjusted units: value is `shares * raw * factor`. In `positions`, `shares` reports `adjusted_shares * factor(date)`, and `fill_price` is the raw open, blank when nothing traded in that symbol. Warn in `positions.warning` for held symbols with no bar and unfilled targets, and in `portfolio.warning` when purchases are scaled. Wrap strategy exceptions in `StrategyError`. History before `start` stays visible to the strategy. Emit a position row for each symbol with a target, a holding or a trade.

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add app/backtest.py tests/test_backtest.py tests/test_reconciliation.py
git commit -m "feat: run strategies under self-financing cash rules"
```

---

### Task 6: Strategy files, results and the command

**Files:**
- Modify: `app/backtest.py`
- Create: `tests/fixtures/buy_spy.py` (the dummy strategy)
- Test: `tests/test_backtest.py`

**Interfaces:**
- Consumes: `run`, `Result`, `StrategyError` from Task 5; `Store`, `refresh_daily`, `load_bars`, `universe`, `default_db_path` from Tasks 2 and 3.
- Produces, in `app/backtest.py`:
  - `load_strategy(path: str) -> Callable`: loads a file by path, and raises `StrategyError` if `target_weights` is missing
  - `write_results(result: Result, out_dir: str, source: str) -> None`: writes `portfolio.csv` and `positions.csv`, creating the directory
  - `main(argv=None) -> int`
- Command: `uv run python -m app.backtest <strategy.py> --start YYYY-MM-DD --end YYYY-MM-DD --out DIR [--capital N] [--cost-bps N] [--dividends | --no-dividends]`

- [ ] **Step 1: Write the failing tests**

```python
def test_load_strategy_runs_the_dummy():
    fn = load_strategy("tests/fixtures/buy_spy.py")
    assert fn({}, None) == {"SPY": 1.0}

def test_load_strategy_without_function_is_a_clear_error(tmp_path):
    path = tmp_path / "s.py"
    path.write_text("x = 1\n")
    with pytest.raises(StrategyError, match="target_weights"):
        load_strategy(str(path))

def test_results_carry_the_required_note(tmp_path):
    write_results(run(all_in("A"), plain, D[0], D[3]), str(tmp_path), "Alpaca SIP daily")
    for name in ("portfolio.csv", "positions.csv"):
        first = (tmp_path / name).read_text().splitlines()[0]
        assert first.startswith("#") and "survivorship bias" in first
        assert "Alpaca SIP daily" in first and "Dividends: on" in first
    assert len(pd.read_csv(tmp_path / "portfolio.csv", comment="#")) == 4

def test_main_runs_end_to_end(tmp_path, monkeypatch):
    # Patch the key loader, client and db path; the fake client serves SPY bars
    assert main(["tests/fixtures/buy_spy.py", "--start", D[0], "--end", D[3], "--out", str(tmp_path)]) == 0
    assert (tmp_path / "portfolio.csv").exists()

def test_main_without_a_key_explains_what_to_do(tmp_path, monkeypatch, capsys):
    # load_keys returns None
    assert main(["tests/fixtures/buy_spy.py", "--start", D[0], "--end", D[3], "--out", str(tmp_path)]) == 1
    assert "Alpaca key" in capsys.readouterr().err

def test_main_reports_a_failed_download(tmp_path, monkeypatch, capsys):
    # client raises an HTTP 403
    assert main([...]) == 1
    assert "Could not download" in capsys.readouterr().err

def test_viewer_does_not_import_the_backtest():
    for name in ("app/ui.py", "app/main.py"):
        text = Path(name).read_text()
        assert "app.backtest" not in text and "app.daily" not in text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_backtest.py -v`
Expected: FAIL on the missing names.

- [ ] **Step 3: Create `tests/fixtures/buy_spy.py`**

A `target_weights(history, as_of)` that returns `{"SPY": 1.0}`.

- [ ] **Step 4: Implement `load_strategy`, `write_results` and `main`**

Note line: `# Today's S&P 500 members only, so results have survivorship bias. Data: {source}. Dividends: {on|off}.`

`main` imports `app.settings` inside the function, because it pulls in Qt. It refreshes daily data, prints progress to stderr, loads bars, runs, and writes with source `"Alpaca SIP daily"`. A missing key, `StrategyError`, `ValueError` or a download failure prints one line to stderr and returns 1. The no-key message: `No Alpaca key found. Open Stock Bars once and enter your Alpaca key.` Add `if __name__ == "__main__": sys.exit(main())`.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/backtest.py tests/fixtures/buy_spy.py tests/test_backtest.py
git commit -m "feat: add backtest command and results files"
```

---

### Task 7: Check real data against known events

Proves the stored Alpaca data, which a case with its own prices would not. This uses the owner's key and the network, so it runs only on request.

**Files:**
- Create: `tests/test_daily_live.py`

**Interfaces:**
- Consumes: `fetch_daily` from Task 3.

- [ ] **Step 1: Write the tests, skipped unless `STOCKBARS_LIVE=1`**

```python
pytestmark = pytest.mark.skipif(os.environ.get("STOCKBARS_LIVE") != "1", reason="needs a real key")

def test_aapl_4_for_1_split():
    assert ratio("AAPL", "factor_split", "2020-08-28", "2020-08-31") == pytest.approx(4, rel=1e-3)

def test_tsla_3_for_1_split():
    assert ratio("TSLA", "factor_split", "2022-08-24", "2022-08-25") == pytest.approx(3, rel=1e-3)

def test_aapl_25_cent_dividend():
    prev_close = raw_close("AAPL", "2024-08-09")
    assert ratio("AAPL", "factor_all", "2024-08-09", "2024-08-12") == pytest.approx(prev_close / (prev_close - 0.25), rel=1e-4)
```

`ratio(symbol, field, before, after)` is `factor(after) / factor(before)`, and fetches through `fetch_daily` with `settings.load_keys()`, using the complete symbols it returns.

- [ ] **Step 2: Run the live tests**

Run: `STOCKBARS_LIVE=1 uv run pytest tests/test_daily_live.py -v`
Expected: 3 passed. On a failure, check the event's date against Alpaca's corporate actions data before suspecting the code, and report the result.

- [ ] **Step 3: Run a real end-to-end backtest**

Run: `uv run python -m app.backtest tests/fixtures/buy_spy.py --start 2020-01-02 --end 2025-12-31 --out <scratchpad>/spy`
Expected: exit 0, and `portfolio.csv` shows a total return close to SPY's published total return for that span. Report the number and the download time.

- [ ] **Step 4: Confirm the default run skips the live tests**

Run: `uv run pytest -q`
Expected: all pass, 3 skipped.

- [ ] **Step 5: Commit**

```bash
git add tests/test_daily_live.py
git commit -m "test: check daily data against known splits and dividends"
```

---

### Task 8: Reconcile with the reviewer's test case (blocked)

**Do not start until the reviewer's case arrives.** Tasks 1–7 finish without it. This stage is not done until this task passes, and the app screen's plan starts only after that.

**Files:**
- Modify: `tests/test_reconciliation.py`
- Create: test data as the case requires

- [ ] **Step 1: Confirm the case is safe for a public repo**

Ask the owner. If it isn't, keep it in a local file and make the test skip when the file is absent.

- [ ] **Step 2: Choose the path, based on the case's prices**

- **It has its own prices:** pass them to `run` directly, and report any differences from Alpaca's prices separately. Task 7 covers stored data.
- **It relies on Alpaca data:** freeze a snapshot of those bars from `load_bars` as the test's input, because refetches can restate adjusted history.

If it needs anything the defaults don't cover, such as shorts, shares, another fill time or a specific dividend setting, stop and bring the change to the owner. Those change the interface and the spec.

- [ ] **Step 3: Write the check and run it**

Assert daily returns match with `abs=1e-6`, unless the reviewer states another tolerance.

Run: `uv run pytest tests/test_reconciliation.py -v`
Expected: PASS. On a mismatch, use superpowers:systematic-debugging and find which side differs and why before changing anything. Do not loosen the tolerance to pass.

- [ ] **Step 4: Commit, then return to the owner**

```bash
git add tests/test_reconciliation.py
git commit -m "test: reconcile backtest with reviewer's case"
```

Report that the module stage is done and the app screen plan can start.
