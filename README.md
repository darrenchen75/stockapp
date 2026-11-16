# Stock Bars

Stock Bars shows you minute-by-minute prices for every company in the S&P 500,
right on your computer. It keeps the last 30 days of prices and updates every
minute while the stock market is open.

It only shows prices. It never touches your real money. The only buying and
selling it can do is pretend trading in an Alpaca paper account (see Paper
trading below), and orders go out only after a person confirms them (**Send** on the
plan shown, or the confirmation that **Sell everything** asks for).

## What you'll see

**On the left: a list of about 500 companies**, by their stock symbol (for
example, AAPL is Apple). For each one you'll see:

- **Last**: the most recent price.
- **% Chg**: how much the price moved since the previous trading day's last
  price. Green means up, red means down.
- **Volume**: how many shares traded on the latest trading day.
- **Gaps**: how many market days in the past month are missing prices for that
  company. Usually 0. A higher number means some of its history is incomplete.

Click any column title to sort the list by it.

**On the right: a price chart** for whichever company you click. Each small
bar is one minute: green if the price rose during that minute, red if it fell.
Use the buttons above the chart to see:

- **1D**: the latest trading day.
- **5D**: the last 5 days.
- **30D**: the last 30 days.

**At the bottom: a status line.** It says **Live** while new prices are coming
in. It says **Market closed** outside trading hours, with the time of the
latest price. If your internet drops, it says **Offline** and keeps trying on
its own.

## What to expect

- **The first time you open it**, the app spends a few minutes downloading a
  month of prices. The status line shows its progress. You can watch the list
  fill in while it works.
- **After that**, opening the app is quick. It only downloads what's new since
  you last used it.
- **Prices update every minute** on weekdays from about 8:00 a.m. to 5:00 p.m.
  New York time. That covers the main market hours (9:30 a.m. to 4:00 p.m.) plus
  some early and late trading. Nights, weekends, and holidays show the last
  prices from before trading stopped.
- **Some minutes have no bar.** Quiet stocks don't trade every minute, so their
  charts can have small empty spaces. This is normal.
- **Volume looks lower than on other websites.** The free price source sees
  only part of the market's trading. Prices are accurate, but volume numbers
  are smaller than the full-market totals you see elsewhere.
- **Stock splits are handled for you.** When a company splits its stock, the
  app fixes that company's past prices so the chart doesn't show a sudden fake
  drop or jump.

## Backtests

Open **Tools**, then **Backtests…** to try a trading idea on past prices. The
app never buys or sells anything in a backtest. A backtest only replays history on paper.

- **Make a run.** Click **New…**, pick a strategy file (a small Python file
  that decides what to hold each day), choose the dates, starting money, a
  trading cost and whether to count dividends, and start it. It runs in the
  background, shows its progress, and you can cancel it. The strategy file may
  use Python's standard library, pandas and numpy.
- **Compare runs.** Every finished run is saved in a list. Tick up to 6 to draw
  them on one chart (each starts at $100,000, so runs with different starting
  money compare fairly) and side by side in a table of total return, yearly
  growth, worst drop, trades and warnings. You can also tick **SPY**, a
  buy-and-hold benchmark for the S&P 500. You can rename or delete a run, re-run it
  with the same settings, open one to see what it held at each rebalance, and export
  its results as two CSV files.
- **Runs stay on this computer.** They are saved in Stock Bars' own data
  folder, along with a copy of the strategy file as it was when you ran it.
  Nothing is sent anywhere or shared between computers. Backtests use
  daily prices, which the app downloads the first time you run one.
- **Read results with care.** A backtest uses today's S&P 500 companies only,
  so it leaves out companies that dropped out of the index. That makes results
  look better than they would have been (survivorship bias). The note on the
  screen, in the run detail and in exports says this, and names the data source
  and the dividend setting. Past results don't predict future ones.

## Strategy contract

A strategy file has one job: given the prices up to a day, say what to hold.
Its answer must depend only on `history` and `as_of`, the two things it is
given. It must not keep state between calls, use unseeded randomness, read
files, read the clock or use the network. The same history must always give
the same answer.

Backtests and paper trading both run your file this way. Paper trading has a
**Replay** button that runs a past day's saved inputs through the saved copy
of the strategy again and checks that it gives the same targets. Replay checks
that the same data and code give the same targets. It can't catch a strategy
whose state resets on each load, such as a module-level counter, because each
run loads the file fresh.

## Paper trading

Open **Tools**, then **Paper trading…** to run a strategy against an Alpaca
paper account. The money is pretend, but the orders are real orders at Alpaca.

- **Paper only.** The app only talks to Alpaca's paper endpoint. Live trading
  is off. The window has a **Live trading** switch, but it is permanently
  disabled: the app has no code signing, so a tampered update could otherwise
  trade real money. A strategy bug that can only lose pretend money is a much
  safer way to find out. If the keys don't reach
  a paper account, the window does nothing, and any orders must be stopped
  from Alpaca's dashboard.
- **Setup.** Use the paper key from Setup below. In the window, confirm the
  account it shows, choose a strategy file and check the limits.
- **When you can run.** The strategy decides after 20:30 New York time on a
  trading day (day T), once that day's prices are in. You can run it any time
  from then until 09:28 New York time on the next trading day (T+1). After
  that the window closes and you wait for the next 20:30. T and T+1 come from
  Alpaca's market calendar, so weekends and holidays are skipped.
- **Run, then Send.** **Run today** makes a plan and saves it. It sells first, then
  buys, and shows each order with the reason for it. Nothing goes to Alpaca
  until you click **Send** on the plan shown. Orders are market orders that fill at
  the next open (Alpaca calls this `opg`).
- **Whole shares.** Alpaca refuses fractional at-the-open orders, so paper
  trades whole shares, sized from day T's close.
- **Limits.** Defaults: no more than 25% of the account in one company, stop
  buying after a 3% one-day loss (sales still go out, and you clear the lock
  yourself), and keep 2% of the money as cash. You can edit all three.
  Each edit is saved in the log.
- **Kill switch.** **Kill switch** stops everything at once: it halts the
  account, cancels every open order and closes the plan. Nothing runs again until you
  click **Release halt**, which works only once every order has finished.
- **Sell everything.** First it halts the account and cancels open orders, after you confirm.
  Then it makes a plan that sells every position and sends the sales only after
  a second confirmation.
- **One computer per account at a time.** Two computers running the same
  account can send the same orders twice. The window warns you if Alpaca shows
  orders this computer has no record of.
- **Logs stay on this computer.** Plans, orders and the log of fills are in
  Stock Bars' own data folder, per account. They are not shared between
  computers.
- **Fills and results.** Paper fills are simulated by Alpaca against quotes.
  The app compares each fill with day T's close, and the compare screen shows
  the paper account as its own line beside your backtests.
- **Dividends.** Two separate things:
  - The account gets no dividends. Alpaca paper accounts don't pay them, so
    paper results leave them out. A backtest with dividends on will look
    better than its paper run.
  - The strategy's input includes dividends by default, the same as a
    backtest's. You can turn this off under **Safety**, **Edit…**.

## Setup

### Step 1: Get a free Alpaca key

The app gets its prices from Alpaca, a free service. You need a "key" from
Alpaca, which works like a username and password for the app.

1. Go to https://app.alpaca.markets and sign up for a free account.
2. On your Alpaca dashboard's home page, find the **API Keys** box and click
   the button to generate a new key. A "paper trading" key is fine. It uses
   no real money.
3. Alpaca shows you a **Key ID** and a **Secret**. Keep that page open, or
   copy both somewhere safe. Alpaca shows the Secret only once.

### Step 2: Download the app

Go to the [download page](https://github.com/darrenchen75/stockapp/releases),
find the newest release at the top, and download the file for your computer.

**Mac** (newer Macs with Apple chips):

1. Download `StockBars-macos.dmg` and double-click it.
2. Drag **StockBars** into your **Applications** folder.
3. Open it from Applications. The first time, your Mac blocks it because the
   app isn't from the App Store. Click **Done**, then allow it:
   1. Open **System Settings**, then **Privacy & Security**.
   2. Scroll down and click **Open Anyway** next to the StockBars message.
      This button only appears for about an hour after the Mac blocks the app.
   3. Enter your Mac password (or use Touch ID).
   4. Click **Open Anyway** again to confirm.

   You only need to do this once.

**Windows:**

1. Download `StockBars-windows.zip`.
2. Right-click the file and choose **Extract All**, then click **Extract**.
3. Open the new folder and double-click **StockBars**.
4. If a blue "Windows protected your PC" box appears, click **More info**, then
   **Run anyway**. You only need to do this once.

**Linux:**

1. Download `StockBars-linux.AppImage`.
2. Right-click the file and open **Properties**.
3. Turn on **Allow executing file as program** (on some systems it's called
   **Executable as Program**).
4. Close the window and double-click the file.

### Step 3: Enter your key

The first time the app opens, it asks for your **Key ID** and **Secret**.
Paste in the two values from Step 1 and click **OK**. The app saves them
securely on your computer, so you won't be asked again. (On some Linux
computers with no password keyring, the app tells you it can't save them, and
asks each time you open it.)

If the app ever says Alpaca rejected your key, make a new key in Alpaca (Step 1)
and paste it in.

## Updates

When a new version is out, the app asks you. Click **Update** and the app
installs it and restarts by itself. Your key and your saved prices stay as
they were. On a Mac, it may ask for your password once after an update;
enter it and click **Always Allow**.

If an update can't install, the app keeps working as before and offers you the
download page, so you can get the new version by hand.

If you have version 0.1.2 or older, those versions can't update themselves.
Download the app once more by hand (Step 2 of Setup). After that, updates come
through the app.

## Something wrong?

Send the app's log file to whoever gave you the app. It's in this folder:

- **Mac**: open Finder, press Shift-Command-G, and paste `~/Library/Logs/stockapp/`
- **Windows**: press Windows key + R, paste `%LOCALAPPDATA%\stockapp\Logs\`, and press Enter
- **Linux**: `~/.local/state/stockapp/log/` in your Home folder (show hidden
  files with Ctrl+H)

## Building the app yourself

Run `packaging/build.sh`. It is the same command the release uses, and the app lands in
`dist/`. To check that strategies can import the standard library, pandas and numpy in
that build, run `dist/StockBars/StockBars --selftest packaging/smoke_strategy.py`. It
prints `selftest ok`.

The viewer's extra lists come from Wikipedia, fetched 2026-11-16:
`app/nasdaq100.csv` from <https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies> and
`app/dow30.csv` from <https://en.wikipedia.org/wiki/List_of_Dow_Jones_Industrial_Average_companies>
(Dow names and sectors come from `app/sp500.csv`). Sectors for members outside the S&P 500 were assigned by hand to GICS sectors.
