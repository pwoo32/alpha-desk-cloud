# SPY 0DTE data collector

Collects SPY same-day-expiry (0DTE) option market data from Robinhood every trading day on GitHub Actions and commits it to `data/<date>/`.
Read-only: the client refuses every request except GET and the OAuth token refresh, and there is no order code here.

| ET | What |
|---|---|
| 09:30 to close | every minute: SPY 1-min bars (`spy_1m.csv`), bid/ask of every 0DTE contract within ±$15 of SPY (`ticks.csv`) |
| 10:00 ... 15:00 | hourly full-chain snapshot with IV, greeks, OI, volume (`intraday_quotes.csv`) |
| close + 12 min | 1-min and 5-min bars of every 0DTE contract (`option_bars.csv.gz`), end-of-day quotes (`quotes.csv`), QQQ/IWM/VIX/SPX 1-min, next expiry quotes, SPY quote and book |

The workflow (`.github/workflows/collect.yml`) runs as two jobs because a hosted job may run at most 6 hours (hand-off at 12:45 ET).
The Robinhood OAuth token is kept encrypted on the `runner-state` branch (`collector/state.py`); the key is the secret `RH_TOKEN_KEY`.
Manual runs: Actions > collect > Run workflow (`day`, `capture`, `selftest`).

Not investment advice. Data is as returned by Robinhood's undocumented API, with no guarantee of completeness.
