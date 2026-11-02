#!/usr/bin/env python3
"""Do chart signals on the underlying forecast where the contract goes? Run: .venv/bin/python chart_signals.py

For every (market, minute) with 2-13 min left: chart signals from the underlying's 1-min closes and from the
contract's own price, then two questions:
  1. residual: does the signal know something the market price doesn't?  corr(signal, outcome - mid)
  2. scalp:    buy the side the signal favours at the ask, sell at the bid 3 minutes later, taker fees both ways
  3. hold:     same entry, hold to settlement
Train/test split by day. Uses backtest_cache.json (15k markets, Coinbase 1-min closes).
"""
import json, math, statistics

import bot
from backtest import CACHE

data = json.loads(CACHE.read_text())
FWD = 3   # minutes until the scalp exit


def rsi(closes, n=14):
    ups = [max(a - b, 0) for a, b in zip(closes[:n], closes[1:n + 1])]
    downs = [max(b - a, 0) for a, b in zip(closes[:n], closes[1:n + 1])]
    return 100 - 100 / (1 + (sum(ups) / n) / max(sum(downs) / n, 1e-12))


rows = []   # (day, signals dict, mid, ask_yes, bid_yes, ask_no, bid_no, fut_mid, outcome)
for series, d in data.items():
    px = d["closes"]
    for m in d["markets"]:
        won = 1.0 if m["result"] == "yes" else 0.0
        q = [(e, float(b), float(a)) for e, b, a in m["quotes"] if 0 < float(b) < float(a) < 1]
        close = q[-1][0] if q else 0
        for i, (end, yb, ya) in enumerate(q):
            mins = (close - end) / 60 + 1
            if not 2 <= mins <= 13 or i + FWD >= len(q):
                continue
            h = [px.get(str(end - 60 * k)) for k in range(61)]
            if None in h:
                continue
            fut = (q[i + FWD][1] + q[i + FWD][2]) / 2
            prev = (q[i - 3][1] + q[i - 3][2]) / 2 if i >= 3 else (yb + ya) / 2
            sig = {"ret 1m": h[0] / h[1] - 1, "ret 5m": h[0] / h[5] - 1, "ret 15m": h[0] / h[15] - 1,
                   "ret 30m": h[0] / h[30] - 1, "ret 60m": h[0] / h[60] - 1,
                   "RSI-14 minus 50": rsi(h) - 50, "above SMA-20": h[0] / (sum(h[:20]) / 20) - 1,
                   "contract trend 3m": (yb + ya) / 2 - prev, "MEAN-REV (minus ret 5m)": -(h[0] / h[5] - 1)}
            rows.append((end // 86400, sig, (yb + ya) / 2, ya, yb, 1 - yb, 1 - ya, fut, won))

days = sorted({r[0] for r in rows})
split = days[len(days) * 2 // 3]
train, test = [r for r in rows if r[0] < split], [r for r in rows if r[0] >= split]
print(f"{len(rows)} market-minutes; train {len(train)}, test {len(test)}\n")


def corr(x, y):
    mx, my = statistics.mean(x), statistics.mean(y)
    sx, sy = statistics.pstdev(x), statistics.pstdev(y)
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (len(x) * sx * sy) if sx and sy else 0.0


def trade(name, rs, hold):
    """Top-quintile |signal| only (the 'clear' chart setups). Buy the favoured side at the ask."""
    vals = sorted(abs(r[1][name]) for r in rs)
    cut = vals[int(len(vals) * 0.8)]
    pnl, staked = [], 0.0
    for day, sig, mid, ya, yb, na, nb, fut, won in rs:
        s = sig[name]
        if abs(s) < cut or s == 0:
            continue
        if s > 0:   entry, exit_, win = ya, fut - (ya - yb) / 2, won           # buy YES at ask, exit at future bid
        else:       entry, exit_, win = na, (1 - fut) - (ya - yb) / 2, 1 - won  # buy NO
        if not 0.05 <= entry <= 0.95:
            continue
        p = (win - entry - bot.fee(1, entry)) if hold else (exit_ - entry - bot.fee(1, entry) - bot.fee(1, max(exit_, 0.01)))
        pnl.append(p); staked += entry
    t = statistics.mean(pnl) / (statistics.stdev(pnl) / math.sqrt(len(pnl))) if len(pnl) > 30 else 0
    return f"{sum(pnl) / staked:+6.1%}/$ t={t:+6.1f} n={len(pnl):6d}"


print(f"{'signal':26s} | residual corr (train/test) | {'SCALP 3-min, train':24s} | {'SCALP test':24s} | {'HOLD test':24s}")
for name in rows[0][1]:
    rc = [corr([r[1][name] for r in rs], [r[8] - r[2] for r in rs]) for rs in (train, test)]
    print(f"{name:26s} |    {rc[0]:+.3f} / {rc[1]:+.3f}       | {trade(name, train, False)} | {trade(name, test, False)} | {trade(name, test, True)}")
print("\nresidual corr: how much of what the market MISSED the signal explains. |0.05| would be a real edge;"
      "\nthe market price itself has corr +%.3f with the outcome." % corr([r[2] for r in test], [r[8] for r in test]))
