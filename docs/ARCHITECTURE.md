# Architecture

```
            Coinbase Exchange (public)            Kalshi trade API v2 (public + signed)
            ticker, 1-min candles                 markets, trades, orders, balance
                     │                                      │
                     ▼                                      ▼
  bot.py   underlying() ──► evaluate() ──► size_order() ──► place_live_order()  (live only, IOC)
             fair_yes()        │               │                   │
                               ▼               ▼                   ▼
                          check_halt()   ──────────────►   journal.db (signals, trades)
                               ▲                                   ▲
  maker.py        plan() ──► paper_fill() / live_place() ──────────┘ (maker_orders)
                  reuses bot.py's caps, halts, journal, settle()
```

| File | Responsibility |
|---|---|
| `bot.py` | HTTP with retries, Kalshi RSA-PSS signing, the fair-value model, risk limits, journal, settlement, the taker loop. |
| `maker.py` | Maker rules (`plan`), the paper fill model (`paper_fill`), paper profiles, live post-only order plumbing, `--report`. |
| `report.py` | Calibration of the taker model against settled outcomes (Brier score, buckets). |
| `test_bot.py`, `test_maker.py` | Offline tests with mocked HTTP and temp journals. |
| Research scripts | One question each; see [RESEARCH.md](RESEARCH.md). They import `bot` for HTTP and fees only. |

## Dry, paper, live

`LIVE_TRADING` must be exactly `true` for real orders; anything else is dry-run (`bot.py`) or paper
(`maker.py`). The mode is written into every journal row (`dry`, `live`, `paper-maker`,
`paper-maker-100`, …), and P&L, the daily stop, and the floor are computed per mode, so simulated and
real money never mix. Dry and paper balances are simulated as the cap plus that mode's settled P&L.

`place_live_order` and `live_place` re-check the flag and the per-trade limit themselves, with an
`if`, not an `assert` (Python's `-O` strips asserts). Paper profiles (`PAPER_PROFILE=100`, `btcd3`)
exit at start-up if `LIVE_TRADING=true`.

## The taker model

`fair_yes()` treats log price as a driftless random walk plus a damped continuation of the last
five minutes' return, and returns P(price at expiry ≥ strike) from the normal CDF. Volatility is the
standard deviation of the last hour's 1-minute log returns. Spot is the Coinbase bid/ask mid from the
live ticker, because the candles endpoint lags by 2-4 minutes; candles are used only for volatility
and the 5-minutes-ago reference. Stale data (last trade over 30 s old, or candles over 10 minutes
old) raises instead of trading.

The model ignores that Kalshi settles on a 60-second average of the CF Benchmarks index, not
Coinbase spot. `MIN_EDGE` was meant to cover that basis; the backtest says the model's mistakes are
much larger than the basis.

## Sizing and limits

`size_order()` budgets `min(MAX_TRADE_COST, ACCOUNT_CAP - open_cost, balance)` and then removes
contracts until price plus Kalshi's rounded-up taker fee fits. The API balance can only lower the
budget, never raise it: a $5,000 account still trades at most $10. The maker bot counts resting
orders as money at work, so even if every resting order filled at once it would stay under the cap.

## The paper fill model

A resting bid at price p on the favorite fills when someone buys the longshot at 1-p. `paper_fill()`
reads the public trade tape since the last check and counts a fill when:

- a longshot buyer paid **more** than 1-p (they swept through our level), or
- enough contracts traded at exactly 1-p to clear everything that was queued ahead of us when the
  order was placed, plus our own size.

It assumes nobody ahead of us ever cancels, so real fills would come sooner, not later. Each trade is
counted once because the order remembers the newest trade time it has seen.

## Live safety

- Orders are never auto-retried (`retries=1`): a timeout could mean the order went through. An
  unknown outcome writes `HALT` and stops.
- Taker orders are immediate-or-cancel at the quoted ask, so there is nothing resting to babysit.
- Maker orders are `post_only` (never pay a taker fee), carry a server-side `expiration_time` at 90% of
  market life, set `cancel_order_on_pause`, and are all cancelled when the bot stops.
- One instance per folder and profile, via `fcntl.flock` on a lock file; the lock dies with the
  process, so there is no stale lock to clean up. `bot.py` and the default `maker.py` share one lock
  because they share one $10 cap.

## Things that bit us

- **A cache that silently truncated.** The maker study kept the newest 8,000 trades per market. Only
  busy Bitcoin markets hit the cap, which dropped exactly the volatile stretches where favorites lose,
  and turned -2% into +12%. Fix: `btc_study.py` fetches the full tape and `--fill-cut` refetches any
  market that still hits a page cap.
- **Coinbase candles lag.** The 1-minute candles endpoint ran 2-4 minutes behind, which is a large
  part of a 15-minute market. Fix: spot from the live ticker's bid/ask mid; candles only for volatility.
- **Strikes in one hour are one bet.** The hourly BTC paper bot took up to 12 strikes per hour; in one
  sharp hour six of them lost together, and that profile finished at -$57. Fix: `btcd3` caps it at 3.
- **Peeking through list order.** Kalshi lists hourly strikes in ascending order, and the lowest
  always settle YES, so choosing strikes by list position leaks the outcome into a backtest. Fix:
  `btcd_strikes.py` keeps the earliest fills in time, never by list position.
- **Float noise in the spread check.** `0.52 - 0.49` is `0.030000000000000027`, which failed a 3¢
  limit. Fix: compare with a `1e-9` tolerance.
- **Backtests vs paper.** The maker rules that looked positive in backtests lost $77 over 262 settled
  paper trades. Fix: none. That's the result.
