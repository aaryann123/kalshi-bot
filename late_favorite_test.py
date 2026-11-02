#!/usr/bin/env python3
"""The YouTube "high-probability scalp" (youtube.com/watch?v=a_3HFhfvaQM): with 2-7 min left, buy the
favorite at LO-HI as a taker, hold to settlement, optional stop when its bid falls to STOP.
Uses backtest_cache.json. Per-contract fee = bot.fee(1, p) (rounded up per order; at 5-10 contracts it's lower).
Run: .venv/bin/python late_favorite_test.py
"""
import json, statistics
from datetime import datetime
import bot
from backtest import CACHE

data = json.loads(CACHE.read_text())
rows = []
for series, d in data.items():
    for m in d["markets"]:
        close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()
        rows.append((series, m["close_time"], m["result"] == "yes", close,
                     [(e, float(b), float(a)) for e, b, a in m["quotes"] if 0 < float(b) < float(a) < 1]))
rows.sort(key=lambda r: r[1])
split = rows[len(rows) * 2 // 3][1]


def run(lo, hi, stop, rs, fee_count=1):
    pnl = []
    for _, _, yes_won, close, q in rs:
        pos = None
        for e, b, a in q:
            left = (close - e) / 60
            if pos is None:
                if not 2 <= left <= 7: continue
                for side, ask in (("yes", a), ("no", 1 - b)):
                    if lo <= ask <= hi:
                        pos = (side, ask); break
            else:
                bid = b if pos[0] == "yes" else 1 - a
                if stop and bid <= stop:
                    pnl.append(bid - pos[1] - 2 * bot.fee(fee_count, pos[1]) / fee_count); pos = None; break
        if pos:
            won = yes_won == (pos[0] == "yes")
            pnl.append(won - pos[1] - bot.fee(fee_count, pos[1]) / fee_count)
    if not pnl: return "  no trades"
    return f"n={len(pnl):5d} win={sum(p > 0 for p in pnl) / len(pnl):5.1%} ret/$={sum(pnl) / len(pnl) / ((lo + hi) / 2):+6.2%}"


print("entry band | stop | TRAIN | TEST (unseen)")
for lo, hi in ((0.80, 0.90), (0.85, 0.95), (0.90, 0.97), (0.80, 0.97)):
    for stop in (0, 0.50, 0.40):
        tr, te = [r for r in rows if r[1] < split], [r for r in rows if r[1] >= split]
        print(f"{lo:.2f}-{hi:.2f} | {stop:.2f} | {run(lo, hi, stop, tr)} | {run(lo, hi, stop, te)}")
btc = [r for r in rows if r[0] == "KXBTC15M"]
print("\nBTC only, 0.80-0.97, stop 0.50, 5-contract fee rounding:", run(0.80, 0.97, 0.50, btc, 5))
