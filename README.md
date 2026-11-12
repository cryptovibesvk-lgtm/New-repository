# NSE Paper Trader (virtual money)

An opening-range-breakout paper trader for NSE stocks. It runs on GitHub Actions
every 5 minutes during market hours and publishes a live dashboard on GitHub Pages.
**Virtual money only. No broker is connected and no real orders are placed.**

Rules: scan Nifty 100 at 08:45 IST, keep the top 5 → opening range 09:15–09:30 →
buy the first 5-minute close above the range high (09:30–15:00) → stop at the range
middle, target 1.5× risk → square off at 15:15. Max 3 trades/day, 25% of capital per
position, ₹10,000 max loss per trade, ₹20,000 daily loss limit. Prices: Yahoo Finance
5-minute bars (free, may lag a little). Charges and 0.05% slippage are included.

**NIFTY 50 and SENSEX** are traded every day with the same rules, as 1 futures lot each
(NIFTY lot from NSE's lot-size file, currently 65; SENSEX 20, a BSE contract, so confirm
it). They use the index level from Yahoo, so a real future's premium is not modelled.
Margin is estimated at 12% of contract value, and F&O charges are included. Index trades
do not count toward the 3-stock limit but do count toward the daily loss limit.

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
