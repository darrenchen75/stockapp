# Stock Bars

Minute bars for the S&P 500 on your desktop. Keeps 30 days of history on this
machine and updates every minute during market hours. Shows data only. It never
places trades.

## Get a key

1. Sign up free at https://app.alpaca.markets.
2. Create an API key (a paper-trading key works).
3. Paste the key and secret when the app asks. They are stored in your OS keychain.

The free plan uses the IEX exchange feed. Prices are representative, but volume
is IEX-only, so it's lower than the whole-market volume you see elsewhere.

## Install

Download the file for your system from the latest GitHub Release.

- **macOS** (Apple Silicon): open `StockBars-macos.dmg` and drag the app to
  Applications. The app is unsigned, so the first launch is blocked: open
  System Settings → Privacy & Security, scroll down, click "Open Anyway",
  then confirm.
- **Windows**: unzip and run `StockBars.exe`. If SmartScreen warns, click
  More info, then Run anyway.
- **Linux**: download `StockBars-linux.AppImage`, right-click it, open
  Properties, and turn on "Allow executing file as program" (or "Executable as
  Program"). Then double-click it.

## Problems

Send the log file:

- macOS: `~/Library/Logs/stockapp/`
- Windows: `%LOCALAPPDATA%\stockapp\Logs\`
- Linux: `~/.local/state/stockapp/log/`

## Develop

```
uv sync
uv run pytest
uv run python -m app.main
```

To release, push a version tag (`git tag v0.1.0 && git push --tags`). GitHub
Actions then builds all three installers onto a Release.

To update the symbol list:
`curl -fsSL https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv -o app/sp500.csv`
