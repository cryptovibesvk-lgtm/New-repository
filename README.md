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

**Weekly schedule** – only that day's strategy trades:

| Day | Index | Strategy |
|---|---|---|
| Monday, Friday | NIFTY | ORB1 |
| Tuesday (NIFTY expiry) | NIFTY | Tuesday sell (SELL_TIME) |
| Wednesday, Thursday | SENSEX | EMA9_21 |

- **ORB1 (1-minute candles)** – mark the first 1-minute candle (09:15) high and low. A
  1-minute close above the high buys an ATM CE, below the low an ATM PE. Stop 20 index
  points (~10 premium points); +20 → stop −10, +30 → break-even, +40 → stop +10, then the
  stop follows every further 10 points. Qty 195, then 325 after a losing first trade.
- **Tuesday sell (SELL_TIME)** – the expiring weekly. 09:16 sell the OTM1 CE and OTM1 PE
  (one strike out of the money each side), 2 lots each, buy back at 14:25. 14:30 sell the
  ATM CE and ATM PE, 2 lots each, buy back at 15:15. Added for capital protection: a pair
  is closed early if its combined premium rises 50% above the premium collected. Needs
  roughly ₹2–3L of margin.
- **EMA9_21 (5-minute candles)** – the 9/21 EMA cross only arms the setup; the cross
  candle is never an entry. CE: a later candle whose wick comes back to the 9 EMA and
  whose body closes above it (a retest closing below is skipped). PE is the mirror.
  Stop = lowest low / highest high of the last 3 candles. Book at +30 points and move the
  stop to break-even; the rest exits only when a candle **body** closes across the 9 EMA.
  5 lots, then 7 lots after a loss.

ORB1 and EMA9_21: a winning trade ends the strategy for the day, a losing trade 1 allows
trade 2, two losses end it. No new entries after 14:30; square-off at 15:15.

Capital: stocks ₹5L and the NIFTY + SENSEX option book ₹5L are separate books. Loss
stops on the option book: ₹10,000 per day, ₹15,000 per week, ₹30,000 per month. NIFTY
lot from NSE (65).

Premiums are **estimates**: Black-Scholes with India VIX as the volatility, measured in
trading time (market minutes left to expiry), plus 0.25% slippage per fill and option
charges. Buying strategies use the next weekly expiry on expiry day; the Tuesday sell uses
the expiring weekly.

## Backtest

`backtest.py` replays the option schedule over every session in the bar archive
(`data/bars/` 5-minute, `data/bars1m/` 1-minute, written by `history.py`), and the stock
ORB over all Nifty 50 stocks (first 3 breakouts a day – a fixed universe, so no hindsight).
The dashboard's **Backtest** tab groups results by day / week / month / year for any date
range, lists every trade (date, time, buy/sell, qty, entry, stop-loss, every exit,
charges, net) and downloads it as CSV.

Free Yahoo data only reaches back ~60 days (5-minute) and ~30 days (1-minute). **For a
full year, import from Zerodha Kite Connect** (read-only market data; needs a Kite Connect
app with historical data). Set your keys in your own terminal, never in chat or git:

    $env:KITE_API_KEY = "your_api_key"; $env:KITE_API_SECRET = "your_api_secret"
    python kite.py login                       # sign in to Kite in the browser
    python kite.py history --days 365 --stocks # NIFTY, SENSEX, VIX (1m + 5m) + Nifty 50 stocks
    python site.py                             # rebuild the backtest, then commit and push

CSV files from elsewhere work too: `python history.py import FILE --symbol NIFTY --interval 1m`.

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
