# NSE Paper Trader (virtual money)

An opening-range-breakout paper trader for NSE stocks. It runs on GitHub Actions
every 5 minutes during market hours and publishes a live dashboard on GitHub Pages.
**Virtual money only. No broker is connected and no real orders are placed.**

Rules: scan Nifty 100 at 08:45 IST, keep the top 5 → opening range 09:15–09:30 →
buy the first 5-minute close above the range high (09:30–15:00) → stop at the range
middle, target 1.5× risk → square off at 15:15. Max 3 trades/day, 25% of capital per
position, ₹10,000 max loss per trade, ₹20,000 daily loss limit. Prices: Yahoo Finance
5-minute bars (free, may lag a little). Charges and 0.05% slippage are included.

## Option strategies on NIFTY and SENSEX (`options.py`)

**Day routing:** NIFTY trades on Monday, Tuesday and Friday; SENSEX on Wednesday and
Thursday. Only that day's index takes trades. An ATM CE (up) or PE (down) is bought.

- **ORB1 (1-minute candles)** – mark the first 1-minute candle (09:15) high and low. A
  1-minute close above the high buys a CE, below the low buys a PE.
  - NIFTY: stop 20 index points (~10 premium points). +20 → stop −10, +30 → break-even,
    +40 → stop +10, then the stop follows every further 10 points. Qty 195, then 325
    after a losing first trade.
  - SENSEX: stop 30 index points. +30 → break-even, +40 → stop +10, then every further
    10 points. Qty 200, then 300 after a losing first trade.
- **EMA9_21 (5-minute candles)** – the 9/21 EMA cross only arms the setup; the cross
  candle is never an entry. CE: a later candle whose wick comes back to the 9 EMA and
  whose body closes above it (a retest closing below is skipped). PE is the mirror.
  Stop = lowest low / highest high of the last 3 candles. Book at +30 points and move the
  stop to break-even; the rest exits only when a candle **body** closes across the 9 EMA.
  5 lots, then 7 lots after a loss.

Each strategy runs on its own: a winning trade ends it for the day, a losing trade 1
allows trade 2, two losses end it. No new entries after 14:30; square-off at 15:15.

Capital: stocks ₹5L and the NIFTY + SENSEX option book ₹5L are separate books. Loss
stops on the option book: ₹10,000 per day (also blocks a new trade whose risk would
cross it), ₹15,000 per week, ₹30,000 per month – after a weekly/monthly stop the book
pauses until the next week/month. On expiry day the next weekly expiry is used. NIFTY
lot from NSE (65); SENSEX lot 20 (BSE, confirm it).

Premiums are **estimates** (Black-Scholes with India VIX, ATM strike, time to the weekly
expiry: NIFTY Tuesday, SENSEX Thursday), not exchange quotes, plus 0.25% slippage per
fill and option charges.

## Backtest

`backtest.py` replays the option strategies over every session in the bar archive
(`data/bars/` 5-minute, `data/bars1m/` 1-minute, written by `history.py`), and the stock
ORB over all Nifty 50 stocks (first 3 breakouts a day – a fixed universe, so no hindsight).
The dashboard's **Backtest** tab groups results by day / week / month / year for any date
range, lists every trade (date, time, qty, entry, stop-loss, every exit, charges, net) and
downloads it as CSV. Free Yahoo data only reaches back ~60 days (5-minute) and ~30 days
(1-minute), so the archive grows by itself every trading day. To backtest a full year now,
export candles (e.g. from Zerodha Kite) and import them:

    python history.py import nifty_1min.csv --symbol NIFTY --interval 1m
    python history.py import nifty_5min.csv --symbol NIFTY
    python history.py import sensex_1min.csv --symbol SENSEX --interval 1m
    python history.py import indiavix_5min.csv --symbol INDIAVIX

## Set up (once)

1. Create a free account at github.com and sign in.
2. **New repository** → name it e.g. `nse-paper-trader` → **Public** (GitHub Pages is
   free for public repos) → *Create repository*.
3. On the empty repo page click **uploading an existing file**, drag in **everything
   inside this folder** (including the `.github` folder), and click *Commit changes*.
4. **Settings → Pages** → Source: *Deploy from a branch* → Branch: `main`, folder:
   `/docs` → *Save*. Your dashboard URL appears there, like
   `https://<your-name>.github.io/nse-paper-trader/`.
5. **Actions** tab → enable workflows if asked → open **Paper trader** →
   **Run workflow** once to check it works (green tick).

## Start / Stop

- **Start:** Actions → Paper trader → *Enable workflow* (it is on after setup).
- **Stop:** Actions → Paper trader → ••• → *Disable workflow*.

The dashboard's "Start / Stop on GitHub" button opens that page.

## Notes

- GitHub's 5-minute schedule often runs late (5–15 min, sometimes more). Results stay
  correct: each run rebuilds the day from real completed candles; only the display lags.
- Pages takes a minute or two to refresh after each run.
- Files: `paper.py` (trader), `site.py` (dashboard export), `dashboard.html` (page),
  `dashboard.py` (optional local server), `paper/` (virtual account + journal),
  `vendor/nse-trader-scanner/` (MIT-licensed scanner from sanT-0/nse-day-trader-skills).
- Educational tooling, not financial advice.
