# Research log

Every study behind the two bots, in the order they were done. All data is public: Kalshi market,
quote and trade history, and Coinbase 1-minute candles. Returns are per dollar staked. Nothing here
is a promise about future results; these are backtests and paper simulations on a few weeks of data.

## 1. Does the taker model make money? (backtest, 2026-09-21)

**No demonstrated edge.** `backtest.py` replays `bot.evaluate()` over settled markets using Kalshi's
per-minute quote history and Coinbase 1-minute prices; `variants.py` tries alternatives with a
train/test split. Over 15,000 markets / 30 days / 5 coins:

- The market is better calibrated than the model on every coin (Brier 0.147 vs 0.153).
- Baseline: ~12,000 trades, 56% wins, **-0.6% to -2% per $ staked**. Claimed edge size didn't predict profit.
- 19 variants (timing windows, momentum on/off, wider vol, higher thresholds, trend agreement, fading the
  model, favorites/longshots): none profitable on both train and unseen test data with t > 2.
- The model does carry signal: fading it loses ~14%/$ vs ~5%/$ for a coin flip. It's worth roughly the
  taker fee + spread (~4-5%/$), so net is about zero. A taker strategy on these markets pays away its edge.

```bash
.venv/bin/python backtest.py 3    # fetch 30 days (cached in backtest_cache.json; delete to refetch)
.venv/bin/python variants.py      # add your own variant to the VARIANTS dict
```

Side experiments on the same cache, all negative after fees: `scalp_test.py` (buy dips inside a
market), `late_favorite_test.py` (buy the late favorite as a taker), `chart_signals.py` (chart
signals on the underlying and the contract).

## 2. Market-wide scan (2026-09-21)

`scan.py` checks whether Kalshi's own prices are beatable: top 80 fast-settling series by volume
(crypto, commodities, weather, indices, economics), ~56,000 settled markets, real bid/ask at 50% and
85% of each market's life, per-series fees, P&L clustered by event, train/test split.

- 711 series-level cells: 21 looked profitable on train (~18 expected by luck), **0 held on test**.
  Pooled by category (128 cells): 5 found, 0 held.
- One robust pattern: **longshots (3-15c) lose 20-80% per $ everywhere** (favorite-longshot bias). The
  mirror trade (buy the 85-97c favorite) nets -2% to +2% after fees: spread + taker fee absorb the bias.
- Conclusion: a taker with public information breaks even at best; any edge sits with makers (resting
  orders that longshot buyers hit).

```bash
.venv/bin/python scan.py 80       # cached per series in scan_cache/; full table in scan_results.json
```

## 3. Maker study (2026-09-21)

`maker_pnl.py` measures what makers actually earned, from the public trade tape (every trade records
the taker side; these fast-settling series charge takers only, so makers pay 0). 4.2M trades, 2,300
markets.

- Generic two-sided market making: ~+0.8%/$ front-of-queue, ~0 back-of-queue. No-go for a slow small bot.
- Selling longshots as maker (taker pays 1-15c) looked positive in all 3 groups, both queue views, train
  and test: +1-2%/$ at 1-5c, +2-6%/$ at 5-15c.
- Never be the maker on the longshot side (takers paying 85c+ are informed): -30% to -96%/$.
- 15-min markets: edge only in 50-90% of market life (negative in first half, zero in last 10%).
- Caveats: risk ~90c to make ~10c (about 1 fill in 20-35 loses the stake); real fill rate unknown.

**Part of this study was wrong** — see section 4. Its cache kept only the newest 8,000 trades per
market, which inflated the Bitcoin numbers.

## 4. Full-tape study (2026-10-09)

`maker_pnl.py`'s cache kept only the newest 8,000 trades of each market. Only KXBTC15M ever hit that
cap, so busy, volatile BTC markets lost their mid-window trades and the BTC numbers were inflated
(same 600 markets: favorite at 80-90c = **+11.8% truncated vs -2.2% on the full tape**). Other series
were complete, so their old results stand. Full write-up: [RESEARCH-2026-10-09.md](RESEARCH-2026-10-09.md).

```bash
.venv/bin/python btc_study.py KXDOGE15M --markets=300   # full tape -> maker_cache/<SERIES>_full.json
.venv/bin/python btc_study.py KXBTCD --events=300 --list-pages=8   # hourly series: sample events, all traded strikes
.venv/bin/python btc_study.py KXBTC15M --fill-cut      # re-fetch markets that hit the page cap
.venv/bin/python btc_spot.py DOGE-USD                  # Coinbase 1-min closes for the spot-feature angles
.venv/bin/python btc_angles.py KXDOGE15M               # 12 angles + bot simulation for one series
.venv/bin/python all15.py                              # every 15-min series side by side + stop-loss tests
.venv/bin/python btcd_strikes.py                       # hourly BTC, one bid per strike
```

maker.py's exact rules, per-$ return, 95% CI over markets: **BTC15M -3.1%**, DOGE15M +1.3% (+5.2%
[+1.4,+8.2] back-of-queue), BNB15M +6.8% [+3.7,+9.6], all 19 fifteen-min series pooled +0.9%
[-0.1,+1.8]. Hourly BTC (KXBTCD), a bid on every strike with the favorite at 85-96c: +2.0%
[+0.5,+3.7]. Stop-losses lose money everywhere except BTC15M; a resting take-profit at 99c is neutral.

These are backtests with a simulated fill model. Section 5 is what happened when the same rules ran
forward on paper.

## 5. Paper trading (2026-09-21 to 2026-10-27)

`maker.py` ran in paper mode (no orders; fills decided from the public trade tape) in several
profiles. Settled paper trades, from the local journal:

| Profile | Rules | Settled | Won | P&L | Staked | Per $ |
|---|---|---|---|---|---|---|
| `paper-maker` | $10 cap, 15-min series | 164 | 147 | -$3.98 | $294.98 | -1.3% |
| `paper-maker-100` | $100 cap, 15-min series | 40 | 34 | -$24.07 | $377.07 | -6.4% |
| `paper-maker-btcd` | hourly BTC, up to 12 strikes/hour | 37 | 28 | -$57.43 | $350.43 | -16.4% |
| `paper-maker-btcd3` | hourly BTC, up to 3 strikes/hour | 21 | 20 | +$8.64 | $198.36 | +4.4% |

Combined: 262 settled paper trades, -$76.84. The win rate is high (87%) and the losses are large,
exactly the shape the maker study warned about: one losing fill wipes out 10-30 winners. The one
positive row is 21 trades, far too few to mean anything. The backtested edges did not show up going
forward. No live order has ever been placed.

The original taker bot (`bot.py`) ran dry for one afternoon: 8 settled trades, +$7.36. That is a
sample of eight and says nothing either way; the 15,000-market backtest in section 1 is the better
evidence.
