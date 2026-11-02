# Kalshi Bot

An experiment in trading Kalshi's short crypto markets with $10: a taker bot, a maker bot, and the
backtests that decided between them. Dry-run and paper mode by default, a hard $10 cap when live,
and an honest answer to "does it make money?": **so far, no.**

## Why

Kalshi lists a new Bitcoin, Ether, Solana, XRP, and Dogecoin "up or down" market every 15 minutes.
They look like they should be predictable from the spot price, and plenty of videos say so. This
repository is what happened when that idea was tested properly: a model, a backtest over 15,000
settled markets, a scan of 56,000 more, a study of 30 million public trades, and paper bots running
the best surviving rules forward. It is published as a worked example of testing a trading idea
without fooling yourself, not as a money maker.

## What it does

- **Taker bot (`bot.py`).** Prices each open 15-minute market as P(price at expiry ≥ strike) from
  Coinbase mid, the last hour's 1-minute volatility, time left, and a damped 5-minute momentum term.
  Buys the cheaper side when the edge after Kalshi's taker fee is at least 8¢, the spread at most 3¢,
  and 2-13 minutes remain. Holds to settlement.
- **Maker bot (`maker.py`).** Rests post-only bids on the favorite side at 85-96¢ (selling the
  longshot to whoever wants it) during 50-90% of a market's life, one order per event, never on the
  longshot side. In paper mode it places nothing and decides fills from the public trade tape,
  conservatively: only when a buyer pays through its price, or after everything queued ahead of it
  has traded.
- **Risk limits that can't be argued with.** $10 total at work whatever the API balance says, $2 per
  trade, 2 open positions, a $3 daily loss stop, and a $1 equity floor that writes a `HALT` file and
  refuses to trade until you delete it. A failed or ambiguous order also halts. Live orders re-check
  the `LIVE_TRADING=true` flag and the limits inside the function that sends them.
- **Research scripts.** A dozen standalone studies (`backtest.py`, `scan.py`, `maker_pnl.py`,
  `btc_study.py`, …) that fetch public data, cache it, and print their tables. Each has its method in
  its docstring.
- **A SQLite journal.** Every market evaluated, every simulated or real trade, every resting maker
  order and why it ended. `report.py` and `maker.py --report` read it.

## Results

Short version, with the full numbers in [docs/RESEARCH.md](docs/RESEARCH.md):

- **The taker model has no edge.** Over 15,000 settled markets the market's own prices were better
  calibrated than the model on every coin (Brier 0.147 vs 0.153), and the baseline lost 0.6-2% per
  dollar after fees. None of 19 variants held up on held-out data.
- **No series beat Kalshi's prices as a taker.** 711 series-level tests across 80 series: 21 looked
  profitable on training data, about what luck predicts, and 0 held on test data.
- **Maker backtests looked mildly positive** (+1% to +7% per dollar on a few series), after one
  earlier result turned out to be a caching bug that inflated Bitcoin by 14 points.
- **Paper trading did not confirm them.** 262 settled paper trades across four profiles:
  87% won, -$76.84 overall. The only positive profile has 21 trades.
- **It has never been run with real money.** The live order path has not been tested against the
  real API.

## Quick start

Requires Python 3.10+ and macOS or Linux. No Kalshi account is needed for dry-run or paper mode.

```bash
git clone https://github.com/aaryann123/kalshi-bot
cd kalshi-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python test_bot.py && .venv/bin/python test_maker.py   # offline, no keys
.venv/bin/python bot.py --once      # one dry-run cycle on live public data; places nothing
.venv/bin/python maker.py --once    # one paper-maker cycle; places nothing
```

To see the reports without running a bot, generate a synthetic journal:

```bash
.venv/bin/python Tools/make-demo-journal.py demo-journal.db
KALSHI_JOURNAL=demo-journal.db .venv/bin/python maker.py --report
```

Live trading needs a Kalshi API key in `.env` (see `.env.example`) and `LIVE_TRADING=true`. Given the
results above, don't.

## Requirements

| | |
|---|---|
| Python | 3.10 or newer |
| Packages | `requests`, `cryptography` (only for signed requests in live mode) |
| OS | macOS or Linux (`fcntl` single-instance lock) |
| Network | Kalshi's public API and Coinbase Exchange's public API |
| Keys | None for dry-run, paper, tests, or research. Kalshi API key + RSA private key for live. |

The research scripts cache what they download. The full-tape study is large: the author's cache
reached about 1 GB.

## How it's organised

```
bot.py              taker bot: model, risk limits, journal, Kalshi + Coinbase HTTP, live IOC orders
maker.py            maker bot: plan, paper fill model, paper profiles, live post-only orders
report.py           calibration report for the taker bot's journal
test_bot.py         offline tests: sizing, daily stop, floor HALT, live-order refusal
test_maker.py       offline tests: fill model, slots, paper-only profiles
backtest.py …       research scripts, one question each (see docs/RESEARCH.md)
Tools/              synthetic demo journal
docs/               ARCHITECTURE.md, BUILDING.md, RESEARCH.md, RESEARCH-2026-10-09.md
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the bots work and why, and
[docs/BUILDING.md](docs/BUILDING.md) for running, configuring, and adding a study.

## Contributing

Pull requests are welcome, especially ones that prove a result here wrong.

- Run both test files before and after a change; they need no network.
- Put new strategy ideas through a train/test split in a script before touching a bot.
- Never commit `.env`, keys, `journal.db`, or the data caches; `.gitignore` covers them.
- Good first projects: settle on CF Benchmarks' index instead of Coinbase spot, a fill-rate
  comparison of paper vs. live with one contract, a report that pools all paper profiles.

## Credits and licence

Kalshi Bot is MIT licensed — see [LICENSE](LICENSE).

- Market data from the [Kalshi API](https://docs.kalshi.com) and
  [Coinbase Exchange API](https://docs.cdp.coinbase.com/exchange/), both public endpoints.
- Kalshi is a trademark of KalshiEx LLC; Coinbase is a trademark of Coinbase, Inc. This project is not
  affiliated with either.

**Not financial advice.** This is an experiment that lost money on paper. Event contracts can lose
their entire stake. If you trade with it, you are responsible for the result and for following the
rules where you live.
