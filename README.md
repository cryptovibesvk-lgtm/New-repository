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

Signals come from the index's 5-minute candles; an ATM CE (up) or PE (down) is bought.

- **ORB5** – mark the first 5-minute candle (09:15–09:20) high and low. A close above the
  high, then a retest (a later candle dips back to the high and still closes above it)
  buys a CE; the mirror below the low buys a PE. Stop = middle of that first candle.
- **EMA9_21** – the 9/21 EMA cross only arms the setup; the cross candle is never an
  entry. CE: wait for a later candle whose wick comes back to the 9 EMA and whose body
  closes above it (a retest candle closing below the 9 EMA is skipped). PE is the mirror.
  Stop = lowest low / highest high of the last 3 candles. Exit: book at +30 and move the
  stop to break-even; the rest exits only when a candle **body** closes across the 9 EMA
  (a wick through it does not exit).

Money management: ORB5 and EMA9_21 run independently. Trade 1 = 5 lots; at +30 index
points book 3 lots and move the stop to break-even (ORB5 then trails 20 points per 20).
If trade 1 makes money, that strategy stops for the day; if it loses, trade 2 = 7 lots
(book 5 at +30). Max 2 trades per strategy per index per day.

Capital: stocks ₹5L and the NIFTY + SENSEX option book ₹5L are separate books.
Capital protection added: no new entries after 14:30; square-off at 15:15; skip a signal
whose stop is wider than 40 NIFTY points (130 SENSEX); loss stops of ₹10,000 per day
(also blocks a new trade whose risk would cross it), ₹15,000 per week and ₹30,000 per
month - after a weekly/monthly stop the option book pauses until the next week/month.
SENSEX thresholds are scaled to its bigger points (100 / 65 / 130). On expiry day the
next weekly expiry is used. NIFTY lot from NSE (65); SENSEX lot 20 (BSE, confirm it).

Premiums are **estimates** (Black-Scholes with India VIX, ATM strike, time to the weekly
expiry: NIFTY Tuesday, SENSEX Thursday), not exchange quotes, plus 0.25% slippage per
fill and option charges. The earlier 1-lot index-futures ORB is switched off.

## Backtest

`backtest.py` replays the option strategies over every session in the bar archive
(`data/bars/`, written by `history.py`); the dashboard's **Backtest** tab groups it by
day / week / month / year for any date range. Free Yahoo 5-minute data only reaches back
~60 days, so the archive grows by itself every trading day. To backtest a full year now,
export 5-minute candles (e.g. from Zerodha Kite) and import them:

    python history.py import nifty_5min.csv --symbol NIFTY
    python history.py import sensex_5min.csv --symbol SENSEX
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
