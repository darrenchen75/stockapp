# Stock Minute Bars Desktop App — Design

Date: 2026-10-20

## Goal

A cross-platform desktop app (macOS, Linux, Windows) that collects and shows
minute-bar market data for the S&P 500. Shareable: anyone downloads an installer,
pastes their own free Alpaca key, and within minutes sees a live table and charts.

## Scope

In:
- Minute bars (OHLCV) for the ~500 S&P 500 symbols.
- Rolling 30 days of history in a local cache, backfilled on launch.
- Live streaming during market hours.
- Sortable table of all symbols + candle chart for the selected symbol.
- Installers for all three OSes built in CI.

Out (deliberately, for now):
- Placing trades (paper or real).
- Trade recommendations / algorithms. Python stack chosen so this can be added
  later as a plain module reading the same database; no hooks built now.
- History beyond 30 days (a retention setting change later; DuckDB if SQLite gets slow).
- Code signing, auto-update.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Data source | Alpaca, free IEX feed, each user's own key | Free minute bars; keys may not be redistributed, so each user brings one |
| Stack | Python + PySide6, managed with `uv` | One language; best ecosystem for a future algorithm; official `alpaca-py` |
| Storage | SQLite | Zero setup, enough for 30 days × 500 symbols (~4M rows, ~300MB) |
| Charts | pyqtgraph | Fast native plotting in Qt |
| Packaging | PyInstaller, GitHub Actions matrix | Builds each OS on its own runner; no need to own three machines |

Known limitation: IEX is one exchange, so volume is lower than consolidated volume.
Prices are representative. Shown as-is; noted in the README.

## Architecture

One process. Modules:

- `app/store.py` — the only module that talks SQL. Database file in the OS
  user data dir (`platformdirs`).
  - Schema: `bars(symbol TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v INTEGER, PRIMARY KEY(symbol, ts))`.
    `ts` is the bar's start time, Unix seconds, UTC.
  - Functions: `upsert(bars)` (`INSERT OR REPLACE`), `latest_ts(symbol)`,
    `bars(symbol, since)`, `prune(days=30)`.
- `app/feed.py` — wraps `alpaca-py`.
  - `backfill()`: for each symbol, fetch from `latest_ts(symbol)` (or now − 30 days
    if none) to now via REST, batching ~100 symbols per request, staying under
    200 requests/min. Upserts after each batch, so an interrupted backfill resumes.
  - `stream()`: IEX websocket on a `QThread`, subscribed to minute bars for all
    symbols. Each bar is upserted, then a Qt signal notifies the UI.
- `app/ui.py` — main window.
  - Left: sortable `QTableView` — symbol, last price, % change today, volume today.
  - Right: pyqtgraph candle chart for the selected symbol, range switch 1D / 5D / 30D.
  - Status bar: connection state, backfill progress, market open/closed.
- `app/settings.py` — first-run dialog for Alpaca key + secret, stored in the OS
  keychain via `keyring`. Never written to disk in plaintext.
- `app/main.py` — entry point; wires the above together.
- `app/sp500.csv` — bundled symbol list, updated by hand.

## Data flow

1. Launch: load key from keychain (show dialog if missing).
2. `prune(30)`, then render the table from the cache.
3. Start `backfill()` in the background; progress in the status bar.
4. If the market is open (Alpaca `/clock`), start `stream()` after backfill.
5. Stream bars → store → signal → table row + chart refresh.

## Error handling

- Bad/missing key (401/403): reopen key dialog with "Key rejected"; no backfill.
- Rate limit (429): wait `Retry-After` (else exponential backoff), retry the same
  batch. Progress is persisted per batch.
- Websocket drop: alpaca-py reconnects; on reconnect, run a gap backfill from each
  symbol's `latest_ts`. Status bar shows "Reconnecting…".
- Market closed: no streaming; table shows last close, labeled "Closed". Market
  hours come from Alpaca `/clock`, so no holiday calendar is maintained.
- Offline at launch: open from cache, retry connection every 30s.
- Symbol with no data (delisted/renamed): log and skip; table shows "—".
- Duplicate bars from backfill/stream overlap: prevented by the primary key + upsert.
- Logs: rotating file in the OS log dir (`platformdirs`), so users can send it
  when reporting problems.

## Testing

pytest, minimum that catches real breakage:
- `tests/test_store.py` — real SQLite in a temp dir: upsert keeps one row per
  (symbol, ts); prune drops rows older than 30 days; `latest_ts` correct.
- `tests/test_feed.py` — fake Alpaca client, no network: batching, resume after a
  429, gap fill after reconnect.
- UI: manual check — launch, paste key, table fills, chart renders.

## Distribution

- `.github/workflows/release.yml`: matrix `macos-latest`, `windows-latest`,
  `ubuntu-latest`; PyInstaller build on version-tag push; uploads `.dmg`,
  zipped `.exe`, `.AppImage` to a GitHub Release. Share that link.
- Unsigned. README explains: macOS right-click → Open; Windows SmartScreen →
  More info → Run anyway. Add signing (Apple $99/yr; Windows cert ~$100–400/yr)
  when sharing beyond friends.

## Layout

```
stockapp/
  app/{main,store,feed,ui,settings}.py
  app/sp500.csv
  tests/{test_store,test_feed}.py
  pyproject.toml
  README.md
  .github/workflows/release.yml
```
