# Paper Trading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a `target_weights` strategy once a day against the user's Alpaca paper account: a person confirms the plan, the app sends at-the-open orders under safety limits, and it logs, recovers, tracks fills and shows the account beside backtests.

**Architecture:** Pure planning rules in `app/paper_plan.py`. On-disk state, journal and log in `app/paper_state.py`. Broker access and the daily step in `app/paper.py`. None of these import Qt. The window in `app/paper_ui.py` only calls them, with network calls on a worker thread like `compare_ui.py`. The strategy is called through the same `decide()` the backtest uses.

**Tech Stack:** Python 3.12, PySide6, alpaca-py ≥ 0.44.0 (`alpaca.trading.TradingClient`), pandas, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-11-paper-trading-design.md`. Read it before any task. Section names below refer to it.

**Building waits for the owner's go.** Task 1 then reports to the owner, and Tasks 2+ start only after the owner has read that report. (Done 2026-11-13: results are in the spec's "First build step", and the owner approved the changes.)

## Global Constraints

- Decision time is 20:30 New York time on day T (`app.daily.CUTOFF`). The send deadline is 09:28 New York time on T+1. T and T+1 come from Alpaca's market calendar.
- Orders are market orders with `time_in_force=opg`, sales first, whole shares.
- Defaults: max position 25% of equity per symbol, daily loss cutoff 3%, cash buffer 2%. All editable, and each edit is logged.
- `TradingClient` is created in exactly one function, `app.paper.make_broker`, always with `paper=True`. No code path takes a live flag.
- `client_order_id` format: `sb-{account_number}-{YYYYMMDD}-a{attempt}-{symbol}`, at most 48 characters.
- Keys never appear in logs, journals, exports or exception text written to disk.
- `app/paper_plan.py`, `app/paper_state.py` and `app/paper.py` never import PySide6.
- The viewer must never import paper modules at startup (same rule as `test_viewer_still_does_not_import_backtest_modules`).
- Paper data lives under `user_data_dir("stockapp")/paper/<account_key>/`, where `account_key = f"paper-{account_number}"`. It stays per computer.
- File writes that must survive a crash use a temp file plus `os.replace`. Journal events are appended to a JSONL file and fsynced.
- Existing viewer, backtest and compare behavior doesn't change, except for the added paper lines. The full suite stays green after every task.

## Review Focus

1. **The clock right at a boundary** (exactly 20:30:00, exactly 09:28:00, the DST change weekend, a holiday from the calendar). A person expects the window to open at 20:30:00 and close at 09:28:00 New York time. Covered in Task 3.
2. **A held position with no daily bar** (a stock bought by hand outside the S&P list, or one halted on day T). The person expects it to be sold to zero if not targeted, with no size check needed, and a note that it has no close. Covered in Task 3.
3. **A fractional position bought by hand.** The person expects the whole-share part to be traded and the fractional remainder named in a note, never a rejected fractional at-the-open order. Covered in Task 3.
4. **A journal cut off mid-write by a crash** (last JSONL line truncated). The person expects recovery to ignore that line with a warning and look up the order at Alpaca, not to crash on start. Covered in Task 4.
5. **Alpaca answering "client_order_id must be unique"** on submit. The person expects this to be treated as "already sent": the order is looked up and its real state recorded, not reported as a failure. Covered in Task 6.

---

### Task 1: Check Alpaca's behaviour on a real paper account (report, then stop)

**Files:**
- Create: `tests/test_paper_live.py` (skipped unless `STOCKBARS_LIVE=1`, like `tests/test_daily_live.py`)
- Create: `.superpowers/sdd/2026-11-11-paper-trading/alpaca-report.md` (git-ignored)

**Interfaces:** Produces nothing that later code imports. Produces facts that later tasks rely on.

The owner authorizes using the stored key against the paper account for this task. Orders placed here must be tiny (1 share of SPY) and cancelled straight away, and the test must cancel everything it opened, even when it fails.

- [ ] **Step 1: Write live checks, one test each**, with the stored keys and `TradingClient(*keys, paper=True)`:
  - `test_account_is_paper`: `get_account()` succeeds, and `account_number` starts with `"P"` (record the actual value pattern).
  - `test_calendar_lists_sessions`: `get_calendar(GetCalendarRequest(start, end))` for the last 10 days returns dates, with holidays missing.
  - `test_opg_order_by_client_id`: outside market hours, submit `MarketOrderRequest(symbol="SPY", qty=1, side=BUY, time_in_force=OPG, client_order_id=<unique>)`, find it with `get_order_by_client_id`, cancel it, and check that its status becomes cancelled.
  - `test_client_id_reuse_after_cancel`: submit again with the same ID after the cancel. Record whether Alpaca rejects it, and the exact error text.
  - `test_duplicate_client_id_error_text`: submit the same ID twice without cancelling. Record the exact error class and message.
  - `test_fractional_opg`: submit `qty=0.5` with OPG. Record accept or reject, then cancel.
  - `test_list_orders_by_day`: `get_orders(GetOrdersRequest(status=ALL, after=<today 00:00 ET>, limit=500))` includes the test orders. This is how attempts are found by ID prefix.
  - `test_portfolio_history_daily`: `get_portfolio_history(GetPortfolioHistoryRequest(period="1M", timeframe="1D"))`. Record the timestamps, equity, base value, and what a new account returns.
  - `test_corporate_actions_reach_next_day`: a `CorporateActionsRequest` with `end` = tomorrow is accepted and returns any announced future splits for a known upcoming split if one exists (otherwise record "no case available").
  - `test_buying_power_with_pending_sale`: only if the account holds a position, record `buying_power` before and after a pending OPG sell. Otherwise record "not checked".
  - Weekend submission: if run on a weekend, check whether OPG is accepted. Otherwise record "not checked".
- [ ] **Step 2: Run them.** `STOCKBARS_LIVE=1 uv run pytest tests/test_paper_live.py -v -s`, then check every test order is gone: `get_orders(status=OPEN)` returns no `sb-test-` orders.
- [ ] **Step 3: Write `alpaca-report.md`.** One line per check: what the spec assumed, what Alpaca did, and the exact error text. Mark every difference that changes a spec rule. Paper dividends can't be observed quickly, so record "assumed from Alpaca docs, not checked".
- [ ] **Step 4: Commit the test file only.** `git add tests/test_paper_live.py && git commit -m "test: check Alpaca paper trading behaviour the design relies on"`
- [ ] **Step 5: STOP.** Report to the controller, who shows the report to the owner. Later tasks start only after the owner says go. If a difference changes a rule, the spec is updated first.

---

### Task 2: One decision function for backtest and paper, and an exact input hash

**Files:**
- Modify: `app/backtest.py` (`run` loop, around lines 175-180)
- Test: `tests/test_backtest.py`

**Interfaces:**
- Produces: `decide(strategy, history: dict[str, pd.DataFrame], day: pd.Timestamp, symbols: set[str]) -> dict[str, float]`. It calls the strategy, wraps any exception as `StrategyError(f"{day:%Y-%m-%d}: strategy raised {type}: {msg}")` (today's exact text), and returns `validate_weights(...)`.
- Produces: `input_hash(history: dict[str, pd.DataFrame]) -> str`, a hex SHA-256.

- [ ] **Step 1: Write the failing tests**
  - `test_run_uses_decide`: monkeypatch `app.backtest.decide` with a spy that records calls and delegates. `run(all_in("A"), plain, D[0], D[3])` calls it once per day.
  - `test_input_hash_is_exact`: build a history with `history_as_of(prepare(plain), D[1])`. Its hash differs after adding `1e-12` to one close, and is equal for a deep copy.
  - `test_input_hash_ignores_column_and_key_order`: reordering the dict keys and the symbol columns gives the same hash.
  - `test_input_hash_sees_nan_positions`: two frames with NaN in different cells hash differently.
- [ ] **Step 2: Run.** `uv run pytest tests/test_backtest.py -k "decide or input_hash" -v` should FAIL (ImportError).
- [ ] **Step 3: Implement both, and make `run` call `decide`.** Hash: for each key in sorted order, sort the frame's rows and columns, then feed the key, the column names, the index as ISO dates, and `to_numpy(dtype="float64").tobytes()` into one `hashlib.sha256`.
- [ ] **Step 4: Run the full suite.** `uv run pytest -q` should pass, with no change to any existing result test.
- [ ] **Step 5: Commit.** `feat: share one decision step and hash a strategy's exact input`

---

### Task 3: Planning rules (pure)

**Files:**
- Create: `app/paper_plan.py`
- Test: `tests/test_paper_plan.py`

**Interfaces:**
- Produces:
```python
@dataclass(frozen=True)
class Limits: max_weight: float = 0.25; loss_cutoff: float = 0.03; cash_buffer: float = 0.02
@dataclass(frozen=True)
class Order: symbol: str; side: str; qty: int; client_order_id: str; ref_price: float  # side "buy"|"sell"; ref_price = T close or nan
@dataclass(frozen=True)
class Note: symbol: str | None; kind: str; text: str  # kind: trim_cap | blocked_loss | blocked_split | no_close | fractional
@dataclass(frozen=True)
class LossCheck: breaches: list[tuple[date, float]]; checked_through: date | None; problem: str | None
def run_window(calendar: list[date], now: datetime) -> tuple[date, date, str | None]  # (T, T+1, reason closed or None)
def order_id(account_number: str, day: date, attempt: int, symbol: str) -> str
def plan_orders(targets: dict[str, float], positions: dict[str, float], equity: float, closes: dict[str, float],
                limits: Limits, loss_locked: bool, split_symbols: set[str],
                account_number: str, day: date, attempt: int) -> tuple[list[Order], list[Note]]
def check_losses(history: list[tuple[date, float]], after: date | None, cutoff: float) -> LossCheck
```

Rules `plan_orders` must follow, from the spec's "The daily step", "Safety limits" and "Whole shares":
- Target shares = `floor(w × equity × (1 − cash_buffer) / close)`. Untargeted held symbols go to 0.
- Cap: a buy never takes a symbol above `floor(max_weight × equity / close)` shares. Sales are never trimmed.
- `loss_locked` drops every buy, with a `blocked_loss` note. A symbol in `split_symbols` gets no order and a `blocked_split` note.
- A held symbol with no close is sold to zero with `ref_price=nan` and a `no_close` note. A targeted symbol with no close isn't bought.
- Fractional holdings trade only whole shares, with a `fractional` note for the remainder.
- Sales come first in the returned list, then buys, each group sorted by symbol.

- [ ] **Step 1: Write the failing tests**
  - `test_window_opens_at_2030_and_closes_at_0928`: with calendar `[Thu 2026-11-05, Fri 11-06, Mon 11-09]`, at Fri 20:29:59 ET → T=Thu, reason not None. At Fri 20:30:00 → (Fri, Mon, None). At Sat 12:00 → (Fri, Mon, None). At Mon 09:27:59 → open. At Mon 09:28:00 → reason not None.
  - `test_window_skips_holiday`: if the calendar has no 2026-11-26, a run on Wed 11-25 at 21:00 gives T+1 = Fri 11-27.
  - `test_window_across_dst_change`: Fri 2026-10-30 20:30 ET to Mon 11-02 09:27 ET is open (DST ends 11-01).
  - `test_order_id_format_and_length`: `order_id("PA3ABCDEFGH", date(2026,11,6), 2, "BRK.B") == "sb-PA3ABCDEFGH-20261106-a2-BRK.B"`, and an assert rejects a result over 48 characters.
  - `test_sizing_uses_buffer_and_floor`: equity 100_000, {"A": 0.5}, close 33 → buy floor(49_000/33) = 1484.
  - `test_cap_trims_buys_only`: {"A": 0.6}, close 10, equity 100_000 → buy 2500 with a `trim_cap` note. When 3000 are already held with a 0.6 target, nothing is sold.
  - `test_untargeted_goes_to_zero_sales_first`: holding B 10 with targets {"A": 0.5} → orders `[sell B 10, buy A …]`.
  - `test_loss_lock_drops_buys_keeps_sales`.
  - `test_split_symbol_blocked`.
  - `test_held_symbol_without_close_is_sold` (Review Focus 2).
  - `test_fractional_holding_trades_whole_shares` (Review Focus 3): holding A 10.5 untargeted → sell 10, with a `fractional` note.
  - `test_losses_checks_every_session_after_last`: history Mon 100, Tue 92, Wed 93, after=Mon, cutoff .03 → breaches [(Tue, -0.08)], checked_through Wed.
  - `test_losses_missing_or_zero_history_is_a_problem`: an empty history, or a zero equity in a needed session, gives `problem` not None.
  - `test_losses_first_run_checks_only_latest_session` (spec "Daily loss cutoff", Alpaca results): history `[(Mon, 0.0), (Tue, 0.0), (Wed, 100_000.0), (Thu, 99_000.0)]`, after=None, cutoff .03 → no breaches, problem None, checked_through Thu. The same history with Thu at 90_000.0 → breaches [(Thu, -0.10)].
  - `test_losses_first_run_after_funding_day_is_not_a_loss`: history `[(Mon, 0.0), (Tue, 100_000.0)]`, after=None → no breaches, problem None, checked_through Tue. A latest session at 0.0 → problem not None.
- [ ] **Step 2: Run.** `uv run pytest tests/test_paper_plan.py -v` should FAIL (ImportError).
- [ ] **Step 3: Implement `app/paper_plan.py`.** Window: T = the latest calendar day with `decision_time(T) <= now`. T+1 = the next calendar day after T. Open iff `now < datetime.combine(T+1, time(9, 28), ET)`.
- [ ] **Step 4: Run.** `uv run pytest tests/test_paper_plan.py -v && uv run pytest -q` should pass.
- [ ] **Step 5: Commit.** `feat: add paper trading planning rules`

---

### Task 4: Per-account state, journal, log and lock

**Files:**
- Create: `app/paper_state.py`
- Test: `tests/test_paper_state.py`

**Interfaces:**
- Consumes: `Limits`, `Order`, `Note` (Task 3).
- Produces:
```python
class Busy(Exception): ...           # another window holds the account lock
class AccountStore:
    def __init__(self, root: Path, account_key: str)  # creates root/account_key
    def lock(self) -> AbstractContextManager          # OS file lock (fcntl.flock / msvcrt.locking); raises Busy
    def settings(self) -> dict                        # {"limits": Limits, "strategy_path": str|None, "dividends": bool}
    def save_settings(self, **changes) -> None        # logs a "settings" event
    def state(self) -> dict    # {"halted": bool, "loss_lock": {"session": iso, "loss": float}|None, "loss_checked_through": iso|None, "cleared": [iso]}
    def save_state(self, **changes) -> None            # logs a "state" event with old and new values
    def attempts(self, day: date) -> list[int]
    def journal(self, day: date, attempt: int) -> "Journal"
    def unresolved(self) -> list[tuple["Journal", str]]  # (journal, client_order_id) "sending" with no result
    def entries(self) -> list["Journal"]                # newest first, for the log list
class Journal:
    folder: Path
    def write_plan(self, plan: dict) -> None   # once only: raises FileExistsError if plan.json exists
    def plan(self) -> dict
    def mark(self, client_order_id: str, status: str, detail: dict | None = None) -> None  # appends to events.jsonl, fsync
    def events(self) -> list[dict]              # skips a truncated last line, logging a warning
    def order_states(self) -> dict[str, str]    # latest status per client_order_id
    def close(self, why: str) -> None           # marks the attempt finished (sent, cancelled, killed, failed, discarded)
    def closed(self) -> str | None              # the `why` given to close(), or None while the attempt is open
```
- Layout: `<root>/<account_key>/settings.json`, `state.json`, `events.jsonl` (account-level), `days/<YYYY-MM-DD>/a<n>/plan.json`, `events.jsonl`, `strategy.py` (copy). The plan dict includes the strategy SHA-256.

- [ ] **Step 1: Write the failing tests**
  - `test_state_survives_reopen`: halt, reopen `AccountStore`, still halted.
  - `test_accounts_do_not_share_state` (spec "Safety limits": per account): halting "paper-A" leaves "paper-B" unhalted.
  - `test_plan_is_written_once`.
  - `test_closed_survives_reopen`: `closed()` is None before `close("sent")`, and "sent" after reopening the `AccountStore`.
  - `test_unresolved_lists_sending_without_result`: mark x "sending" and y "sending", then y "accepted" → unresolved is [x].
  - `test_truncated_last_line_is_skipped` (Review Focus 4): append `{"id": "x", "sta` to events.jsonl → `events()` returns the earlier events and logs a warning.
  - `test_second_lock_raises_busy`: hold `lock()` and open a second `AccountStore` in a subprocess → it raises `Busy`.
  - `test_settings_edit_is_logged`: `save_settings(limits=Limits(max_weight=.2))` adds an account event with old and new values.
  - `test_no_key_written`: monkeypatch `load_keys` to return ("KEYID123", "SECRET456"), run a full save cycle, and grep every file under root: neither string appears.
- [ ] **Step 2: Run.** These should FAIL (ImportError).
- [ ] **Step 3: Implement.** Use temp + `os.replace` for the JSON files, append + `flush` + `os.fsync` for JSONL, and `platformdirs.user_data_dir` only in a `paper_dir()` helper.
- [ ] **Step 4: Run.** `uv run pytest tests/test_paper_state.py -v && uv run pytest -q` should pass.
- [ ] **Step 5: Commit.** `feat: keep paper trading state, journal and log per account`

---

### Task 5: Broker wrapper, verified day-T data, and preparing a plan

**Files:**
- Create: `app/paper.py`
- Modify: `app/daily.py` (`refresh_daily`), `app/store.py` (add `daily_last`)
- Test: `tests/test_paper.py`, `tests/test_daily.py`, `tests/paper_fakes.py` (`FakeBroker`)

**Interfaces:**
- Consumes: `decide`, `input_hash`, `prepare`, `history_as_of`, `load_strategy`, `universe` (Task 2 and existing), Task 3, Task 4.
- Produces:
```python
@dataclass(frozen=True)
class Account: number: str; endpoint: str; equity: float; cash: float; buying_power: float
@dataclass(frozen=True)
class BrokerOrder: client_order_id: str; id: str; symbol: str; side: str; qty: float; filled_qty: float; filled_avg_price: float | None; status: str
class Broker:  # wraps TradingClient and CorporateActionsClient; FakeBroker in tests has the same methods
    def account(self) -> Account
    def calendar(self, start: date, end: date) -> list[date]
    def positions(self) -> dict[str, float]
    def orders_since(self, after: datetime) -> list[BrokerOrder]          # status=all, limit 500
    def order(self, client_order_id: str) -> BrokerOrder | None
    def submit(self, order: Order, tif: str = "opg") -> BrokerOrder      # tif "opg"|"day"; errors propagate unchanged
    def cancel_all(self) -> list[tuple[str, bool]]
    def equity_history(self) -> list[tuple[date, float]]                 # 1D portfolio history, each point dated by its New York date (Alpaca stamps 20:00 ET)
    def splits(self, symbols: list[str], start: date, end: date) -> dict[str, date]  # effective dates
    def market_open(self) -> bool                                        # get_clock().is_open
def make_broker(keys: tuple[str, str]) -> Broker   # the only TradingClient(..., paper=True)
def account_key(account: Account) -> str           # f"paper-{account.number}"
@dataclass(frozen=True)
class Refusal: reason: str
@dataclass(frozen=True)
class Plan: account_number: str; day: date; next_day: date; attempt: int; orders: list[Order]; notes: list[Note]
            targets: dict[str, float]; input_hash: str; data_date: str; downloaded_at: str
            account: Account; positions: dict[str, float]; limits: Limits; strategy_sha256: str
            dividends: bool; loss: LossCheck; last_bars: dict[str, dict]  # day-T bar per held or targeted symbol
def prepare_plan(broker: Broker, acct: AccountStore, store: Store, data_client, actions_client,
                 now: datetime) -> Plan | Refusal
```
- `refresh_daily(..., expect: date | None = None)` and `Store.daily_last(symbol) -> str | None`. With `expect`, a current marker doesn't skip the download if SPY's last bar is before `expect`. After a download, SPY's last bar before `expect` raises `IncompleteDownload` and sets no marker. With `expect=None`, nothing changes.

`prepare_plan` follows the spec's daily step 1-9, in order. It refuses (no orders, no journal) when: the account is halted; the window is closed; SPY has no day-T bar; the corporate-actions query fails; open orders exist from another day or attempt; or the strategy file is missing or raises. It loads the strategy fresh. The attempt is 1 + the highest attempt found locally *or* in `orders_since(T 00:00 ET)` by ID prefix. If the latest attempt for day T is not closed and has unsent orders, it returns that attempt's saved plan, limited to the unsent orders (spec "The journal and recovery"). If Alpaca has day-T `sb-` orders with no local journal, it adds a warning note ("another computer may be running this account"). Loss check: `check_losses(broker.equity_history(), state["loss_checked_through"], limits.loss_cutoff)` (the stored ISO date converted to `date`), minus sessions in `state["cleared"]`. A breach or a problem sets the lock and blocks buys. It saves the plan with `journal.write_plan` and copies the strategy file before returning.

- [ ] **Step 1: Write the failing tests** (FakeBroker in memory, store seeded like `tests/test_compare_ui.py`'s `make_window`)
  - `test_daily_expect_forces_download_when_spy_stale` and `test_daily_expect_raises_without_marker` in `tests/test_daily.py`.
  - `test_make_broker_is_always_paper`: monkeypatch `TradingClient` to record its kwargs → `paper=True`. A source check finds `TradingClient(` exactly once in `app/`.
  - `test_plan_matches_backtest_targets`: with `buy_spy`, the plan's targets equal those of `backtest.run`'s last decision on the same bars.
  - `test_refuses_when_halted`, `test_refuses_outside_window`, `test_refuses_without_day_t_spy_bar`, `test_refuses_when_split_lookup_fails`, `test_refuses_with_other_days_open_orders`.
  - `test_split_on_next_day_blocks_symbol`.
  - `test_attempt_counts_broker_orders`: the broker has `sb-…-a1-SPY` for T and there's no local journal → attempt 2, plus the "another computer" note.
  - `test_unsent_remainder_is_offered`: journal a1 has SPY "accepted" and AAPL nothing → the plan holds only AAPL, with the same ID and qty.
  - `test_loss_breach_while_closed_locks`: equity history with an 8% drop two sessions back → `loss_lock` set and no buys.
  - `test_cleared_breach_does_not_relock_but_new_one_does`.
  - `test_plan_saved_before_return`: `plan.json` exists and holds the input hash, strategy SHA-256, dividends setting, loss check result and day-T bars of held and targeted symbols.
- [ ] **Step 2: Run.** These should FAIL.
- [ ] **Step 3: Implement**, in `app/daily.py`, `app/store.py`, then `app/paper.py`. `Broker` maps alpaca-py objects to the dataclasses. Splits come from the forward and reverse split types already used in `app/daily.py`.
- [ ] **Step 4: Run.** `uv run pytest tests/test_paper.py tests/test_daily.py -v && uv run pytest -q` should pass.
- [ ] **Step 5: Commit.** `feat: prepare a confirmed-ready paper plan from verified day T data`

---

### Task 6: Sending, the send-time recheck and crash recovery

**Files:**
- Modify: `app/paper.py`
- Test: `tests/test_paper.py`

**Interfaces:**
- Produces:
```python
@dataclass(frozen=True)
class SendResult: sent: list[BrokerOrder]; failed: list[tuple[str, str]]; discarded: str | None  # discarded = recheck reason
def send_plan(broker: Broker, acct: AccountStore, plan: Plan, now: datetime) -> SendResult
def recover(broker: Broker, acct: AccountStore) -> list[str]   # one line per resolved order, for the window
```
`send_plan` runs while holding `acct.lock()`. First, the recheck (spec daily step 10): before 09:28 on `plan.next_day`; same account number; not halted; loss lock unchanged; limits unchanged; no open orders except this attempt's; positions equal `plan.positions`. Any difference closes the journal as "discarded" and returns `discarded`. Otherwise, for each order in plan order, it checks `state()["halted"]`, marks "sending", submits, then marks the result. A duplicate `client_order_id` error is treated as already sent: `broker.order(id)` is looked up and its status recorded. It is recognised by the substring `client_order_id must be unique` in `str(error)`. Alpaca's real response (Task 1) is HTTP 422 `{"code":42210000,"message":"client_order_id must be unique"}`, raised by alpaca-py as `APIError`. It closes the journal as "sent" when every order has a result.

- [ ] **Step 1: Write the failing tests**
  - `test_send_submits_in_order_with_journal`: events go sending → accepted per order, sales before buys.
  - `test_send_rechecks_positions`: change FakeBroker's positions after prepare → `discarded` not None, and nothing submitted.
  - `test_send_after_deadline_discarded`: now = T+1 09:28 → discarded.
  - `test_send_stops_when_halted_midway`: FakeBroker sets the halt after the first submit → the second order is not sent.
  - `test_duplicate_id_counts_as_sent` (Review Focus 5): FakeBroker raises `Exception('{"code":42210000,"message":"client_order_id must be unique"}')` → no failure, status from `order()`.
  - `test_recover_resolves_sending`: journal x "sending" with no result, FakeBroker knows x as accepted → `recover` marks "accepted"; for an ID Alpaca doesn't know, it marks "not_found".
  - `test_send_twice_never_doubles`: calling `send_plan` twice with the same plan leaves FakeBroker holding one order per ID.
- [ ] **Step 2: Run.** These should FAIL.
- [ ] **Step 3: Implement** `send_plan` and `recover`.
- [ ] **Step 4: Run.** `uv run pytest tests/test_paper.py -v && uv run pytest -q` should pass.
- [ ] **Step 5: Commit.** `feat: send paper orders with a recheck, a journal and recovery`

---

### Task 7: Kill switch, release and sell everything

**Files:**
- Modify: `app/paper.py`
- Test: `tests/test_paper.py`

**Interfaces:**
- Produces:
```python
def kill(broker: Broker, acct: AccountStore) -> list[tuple[str, str]]       # (client_order_id, final status)
def unresolved_cancels(broker: Broker, acct: AccountStore) -> list[str]      # ids not yet cancelled/filled/rejected
def release(broker: Broker, acct: AccountStore) -> str | None                # None = released; else why not
def plan_sell_all(broker: Broker, acct: AccountStore, now: datetime) -> Plan | Refusal
def send_sell_all(broker: Broker, acct: AccountStore, plan: Plan, now: datetime) -> SendResult
```
`kill` saves `halted=True` first, then calls `cancel_all`, then polls each open order's status up to a short timeout and closes the open attempts as "killed". `release` refuses while `unresolved_cancels` isn't empty. `plan_sell_all` is a journaled plan (attempt numbering as in Task 5, IDs `…-a{n}-{symbol}`). It requires the halt to be set (it sets it if not), refuses until there are no open orders, and sells every whole share. `send_sell_all` is like `send_plan` but skips the halt check and calls `broker.submit(order, tif="day")` when `broker.market_open()`, otherwise `tif="opg"`.

- [ ] **Step 1: Write the failing tests**
  - `test_kill_halts_before_cancelling`: FakeBroker records the halt state at the moment of `cancel_all` → already True.
  - `test_release_waits_for_cancels`: one order is stuck "pending_cancel" → `release` returns a reason. Once it's cancelled, it returns None.
  - `test_new_attempt_after_release`: prepare after a kill and release → attempt 2, with quantities recomputed from current positions.
  - `test_sell_all_waits_for_open_buys`: an open OPG buy exists → `plan_sell_all` refuses until it's finished.
  - `test_sell_all_survives_restart`: crash after the first sale is "sending" → `recover` resolves it, and the remainder is offered.
- [ ] **Step 2: Run.** These should FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** `uv run pytest tests/test_paper.py -v && uv run pytest -q` should pass.
- [ ] **Step 5: Commit.** `feat: add kill switch, release and sell everything`

---

### Task 8: Fills, drift reasons, replay and CSV export

**Files:**
- Modify: `app/paper.py`
- Test: `tests/test_paper.py`

**Interfaces:**
- Consumes: `rebalance` from `app/backtest.py`.
- Produces:
```python
def track_fills(broker: Broker, acct: AccountStore, store: Store) -> pd.DataFrame
    # columns: day, symbol, side, ordered, filled, avg_price, open_next, close_t, bps_vs_open, bps_vs_close,
    #          backtest_shares, planned_shares, sent_shares, filled_shares, reasons
def cost_summary(fills: pd.DataFrame) -> dict[str, float]   # traded-value-weighted bps_vs_open and bps_vs_close
def negative_cash_days(broker: Broker) -> list[tuple[date, float]]
def missed_sessions(acct: AccountStore, calendar: list[date]) -> list[date]  # sessions since the first run with no sent attempt
def replay(acct: AccountStore, journal: Journal, store: Store) -> dict  # {"hash_matches": bool, "same_targets": bool|None, "targets": dict|None}
def export_fills(fills: pd.DataFrame, path: str) -> None     # temp + os.replace
```
- Signed bps (spec "Fill tracking"): buy = `(avg − ref)/ref × 10_000`; sell = `(ref − avg)/ref × 10_000`, so positive is always a cost.
- `backtest_shares`: the fractional shares `rebalance` would hold after T+1's open with the plan's targets, sized from the account value at T+1's open (positions × open + cash).
- `reasons`: a `;`-joined list from {cash_buffer, rounding, size_cap, loss_lock, split_block, unfilled, missed_session}, taken from the plan's notes and the fill.
- Rows are recorded once each and stay fixed after T+1's daily bar exists.

- [ ] **Step 1: Write the failing tests**
  - `test_signed_bps_cost_positive_both_sides`: buy at 101 vs open 100 → +100. Sell at 99 vs open 100 → +100.
  - `test_cost_summary_weights_by_value`.
  - `test_reasons_name_rounding_and_cap`.
  - `test_unfilled_opg_recorded`: filled 0, with an `unfilled` reason.
  - `test_replay_matches_then_flags_changed_data`: replay right after prepare → hash_matches True, same_targets True. After `store.replace_daily` changes one close → hash_matches False, targets None.
  - `test_impure_strategy_shows_as_mismatch`: a strategy with a module-level counter → same_targets False.
  - `test_export_fills_round_trips`.
  - `test_missed_sessions_lists_days_without_a_sent_attempt`.
- [ ] **Step 2: Run.** These should FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** `uv run pytest tests/test_paper.py -v && uv run pytest -q` should pass.
- [ ] **Step 5: Commit.** `feat: track paper fills, drift reasons and replay`

---

### Task 9: Paper lines in the compare screen

**Files:**
- Modify: `app/paper.py`, `app/compare_ui.py`
- Test: `tests/test_paper.py`, `tests/test_compare_ui.py`

**Interfaces:**
- Produces: `paper_lines(acct: AccountStore, history: list[tuple[date, float]]) -> list[tuple[str, pd.DataFrame]]`, giving (name, portfolio with `date`, `value`) per strategy segment. The name is `"Paper: <strategy file stem> (<first 7 of sha256>)"`.
- Segments (spec "Performance in the compare screen"): a segment is a strategy SHA-256. It starts at the close of the session before its first filled order and ends at the close before the next segment's first fill. History before the first sent run is dropped.
- `CompareWindow.__init__` gains `paper_source: Callable[[], list[tuple[str, pd.DataFrame]]] | None = None`. When given, a worker fetches it off the UI thread (same pattern as `BenchmarkWorker`). Each line is pinned under SPY, unticked by default, uses SPY-style "never best" rules, gets the same table rows, including `risk()` against SPY, and adds "no dividends (Alpaca paper)" to the results note when ticked.

- [ ] **Step 1: Write the failing tests**
  - `test_segment_starts_at_prior_close`: first fill on Tue → the segment's first row is Mon's close.
  - `test_code_edit_starts_new_segment`.
  - `test_paper_line_pinned_never_best`: with `paper_source` returning one line that beats every run, its column is never bold and it's listed after SPY.
  - `test_paper_note_mentions_no_dividends`.
  - `test_compare_without_paper_source_unchanged`: the existing tests pass unmodified.
- [ ] **Step 2: Run.** These should FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** `uv run pytest tests/test_compare_ui.py tests/test_paper.py -v && uv run pytest -q` should pass.
- [ ] **Step 5: Commit.** `feat: show the paper account beside backtests`

---

### Task 10: The paper window

**Files:**
- Create: `app/paper_ui.py`
- Modify: `app/ui.py` (Tools menu, near line 154, plus `open_paper` mirroring `open_backtests`)
- Test: `tests/test_paper_ui.py`

**Interfaces:**
- Consumes: Tasks 4-9.
- Produces: `PaperWindow(QWidget)` with `__init__(self, db_path: str, root: Path, make_broker: Callable[[], Broker], make_data_clients: Callable[[], tuple], app_version: str)`.

What it shows comes from the spec's "The paper window". Required behavior:
- On open: `recover()` runs on a worker, then the account check. The account number and a "Paper" badge are shown. The first time, the user confirms the account and picks the strategy file. If the account number differs from the last confirmed one, any unsent plan is discarded and the user confirms again.
- Run today is enabled only when `run_window` is open. The disabled tooltip gives the reason.
- Send is enabled only while a plan from this session is shown. Cancel closes the journal as "cancelled".
- The red banner shows while `loss_lock` is set, with Clear (it logs and records the session in `cleared`).
- The kill switch is always enabled once an account is set up. Sell everything asks for confirmation. Unresolved cancels are listed in red.
- A live-trading checkbox is always disabled. Its tooltip: "Live trading is off: the app has no code signing, so a tampered update could trade real money."
- The log list is newest first. A detail view shows the plan, events, fills, and a Replay button.
- Shows the cost summary, negative-cash days and missed sessions.
- All network work runs off the UI thread. Closing while a send runs asks first, like the compare window.
- Tools menu: "Paper trading…" opens one window, imported lazily.

- [ ] **Step 1: Write the failing tests** (offscreen Qt, FakeBroker)
  - `test_run_disabled_outside_window_with_reason`.
  - `test_send_needs_shown_plan`.
  - `test_banner_and_clear`.
  - `test_live_switch_always_disabled`.
  - `test_kill_switch_halts_and_disables_run`.
  - `test_account_change_discards_plan`.
  - `test_tools_menu_opens_one_paper_window`.
  - `test_viewer_still_does_not_import_paper_modules`: extend the existing import guard to `app.paper*`.
- [ ] **Step 2: Run.** These should FAIL.
- [ ] **Step 3: Implement.** Follow `compare_ui.py`'s worker and signal patterns. Wire `paper_lines` into `CompareWindow` from `MainWindow.open_backtests` when a paper account is set up.
- [ ] **Step 4: Run.** `uv run pytest tests/test_paper_ui.py -v && uv run pytest -q` should pass.
- [ ] **Step 5: Commit.** `feat: add the paper trading window`

---

### Task 11: Docs, packaging and a live end-to-end check

**Files:**
- Modify: `README.md`, `StockBars.spec` (only if `alpaca.trading` isn't bundled), `packaging/smoke_strategy.py` (only if needed)
- Test: `tests/test_paper_live.py` (add one end-to-end test)

- [ ] **Step 1: README.** Add a "Strategy contract" section with the spec text: output depends only on `history` and `as_of`, with no state, unseeded randomness, files, clock or network. Add a "Paper trading" section covering: setup; the run window 20:30 to 09:28 New York time; Send; the limits and defaults; the kill switch and sell everything; one computer per account at a time; logs per computer; no dividends on Alpaca paper; live trading is off and why.
- [ ] **Step 2: Packaging.** Build with `packaging/build.sh`, then run the packaged app's `--selftest`. Add `python -c "import alpaca.trading"` coverage by importing `app.paper` in `selftest` (in `app/backtest.py`). Fix `StockBars.spec` hidden imports if the import fails.
- [ ] **Step 3: Live end-to-end** (owner's go required, paper account, the owner picks the moment): add `test_paper_day_end_to_end` to `tests/test_paper_live.py`. Inside the run window, `prepare_plan` with `buy_spy` → `send_plan` → `kill` → `release`. Assert the plan's single SPY order was accepted and then cancelled, and the journal reads sent → killed.
- [ ] **Step 4: Full suite.** `uv run pytest -q` should pass, with the live tests skipped unless `STOCKBARS_LIVE=1`.
- [ ] **Step 5: Commit.** `docs: explain paper trading and the strategy contract`
