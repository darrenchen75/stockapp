# Stock Bars

Stock Bars shows you minute-by-minute prices for every company in the S&P 500,
right on your computer. It keeps the last 30 days of prices and updates every
minute while the stock market is open.

It only shows prices. It never buys or sells anything, and it never touches
your money.

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
they were.

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
