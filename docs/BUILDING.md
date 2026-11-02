# Building and running

## Requirements

| | |
|---|---|
| Python | 3.10 or newer |
| Packages | `pip install -r requirements.txt` (`requests`, `cryptography`) |
| OS | macOS or Linux |
| Keys | Only for live mode or `--balance` |

## First run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python test_bot.py
.venv/bin/python test_maker.py
.venv/bin/python bot.py --once        # dry-run: evaluates open markets, logs, places nothing
.venv/bin/python maker.py --once      # paper: logs the bids it would rest, places nothing
.venv/bin/python maker.py --report    # fill rate and P&L per journal mode
.venv/bin/python report.py dry        # taker model calibration (fetches settled outcomes)
```

Drop `--once` to loop (`bot.py` every 30 s, `maker.py` every 20 s). Paper profiles run alongside the
default bot with their own journal mode, lock, log, and HALT file:

```bash
PAPER_PROFILE=100 .venv/bin/python maker.py     # $100 cap, ~10 contracts per trade
PAPER_PROFILE=btcd3 .venv/bin/python maker.py   # hourly BTC, up to 3 strikes per hour
```

## Configuration

`.env` in this folder is read at start-up (copy `.env.example`); real environment variables win.

| Variable | Meaning |
|---|---|
| `LIVE_TRADING` | `true` sends real orders. Anything else is dry-run or paper. |
| `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` | Kalshi API key and path to its RSA private key. Keep the key outside this folder. |
| `NOTIFY_URL` | Optional webhook. Hard stops POST `{"text": "..."}` here. |
| `KALSHI_JOURNAL` | Journal path, default `journal.db` here. Point it at a demo journal to try the reports. |
| `PAPER_PROFILE` | `100` or `btcd3`: a paper-only profile for `maker.py`. |

Risk and strategy constants are at the top of `bot.py` (`ACCOUNT_CAP`, `MAX_TRADE_COST`,
`DAILY_LOSS_LIMIT`, `BALANCE_FLOOR`, `MIN_EDGE`, …) and `maker.py` (`SERIES`, `MIN_FAV`, `MAX_FAV`,
`LIFE_LO`, `LIFE_HI`, `MAX_SLOTS`, `PROFILES`).

## Files it creates

| File | What |
|---|---|
| `journal.db` | SQLite: `signals`, `trades`, `maker_orders` |
| `bot.log`, `maker.log`, `maker-<profile>.log` | Everything printed to the console |
| `HALT`, `HALT-<profile>` | Present = trading stopped; read it, fix the cause, delete it |
| `.lock`, `.lock-<profile>` | Single-instance locks (empty files) |
| `backtest_cache.json`, `scan_cache/`, `maker_cache/`, `*_output.txt` | Research data and results |

All of them are in `.gitignore`.

## Layout

```
bot.py, maker.py, report.py       bots and the calibration report
test_bot.py, test_maker.py        offline tests
backtest.py, variants.py          taker backtest and variants (train/test)
scalp_test.py, late_favorite_test.py, chart_signals.py   taker side experiments
scan.py                           market-wide scan of 80 series
maker_pnl.py                      maker earnings from the trade tape (8k-trade cache; see RESEARCH.md)
btc_study.py, btc_spot.py, btc_angles.py, all15.py, btcd_strikes.py   full-tape maker studies
Tools/make-demo-journal.py        synthetic journal for trying the reports
docs/                             ARCHITECTURE, BUILDING, RESEARCH notes
```

## Adding a study

Write a script that fetches through `bot.http` / `bot.kalshi` (they handle retries and rate limits),
caches raw data next to it, and prints a table. Split by time into train (first two thirds) and test
(last third), pick rules on train only, and report test with a confidence interval. Add the cache to
`.gitignore` and a section to `docs/RESEARCH.md`.

## Demo data

```bash
.venv/bin/python Tools/make-demo-journal.py demo-journal.db
KALSHI_JOURNAL=demo-journal.db .venv/bin/python maker.py --report
```

The demo journal holds 240 seeded random paper orders on two series. Its numbers mean nothing.

## No screenshots

Kalshi Bot is a command-line program; its output is log lines and printed tables, shown above.

## Not supported

Windows (the lock uses `fcntl`). Settlement-accurate pricing (CF Benchmarks' index rather than
Coinbase spot).
