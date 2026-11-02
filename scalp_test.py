#!/usr/bin/env python3
"""Buy the dip, sell the rip, inside a 15-min market: does it pay after fees?
Uses backtest_cache.json (per-minute yes bid/ask for ~15k settled markets). Run: .venv/bin/python scalp_test.py

Rule: at each minute, if the side's ask has fallen >= DIP from its high over the last LOOK minutes, buy at
the ask. Sell at the bid once it is >= entry + TAKE, or at STOP below entry, or hold to settlement.
Taker fee paid on entry and on exit. One position per market. Train = first 2/3 of days, test = last 1/3.
"""
import json, math, statistics
from itertools import product

import bot
from backtest import CACHE

data = json.loads(CACHE.read_text())
markets = []
for series, d in data.items():
    for m in d["markets"]:
        q = [(e, float(b), float(a)) for e, b, a in m["quotes"] if 0 < float(b) < float(a) < 1]
        if len(q) >= 8:
            markets.append((m["close_time"], m["result"] == "yes", q))
markets.sort()
split = markets[len(markets) * 2 // 3][0]


def run(dip, look, take, stop, side, rows):
    pnl = []
    for _, won_yes, q in rows:
        # quotes for the chosen side: (bid, ask)
        s = [(b, a) if side == "yes" else (1 - a, 1 - b) for _, b, a in q]
        pos = None
        for i in range(look, len(s)):
            bid, ask = s[i]
            if pos is None:
                hi = max(a for _, a in s[i - look:i])
                if hi - ask >= dip and 0.05 <= ask <= 0.95:
                    pos = ask
            else:
                if bid >= pos + take or (stop and bid <= pos - stop):
                    pnl.append((bid - pos - bot.fee(1, pos) - bot.fee(1, bid), pos)); pos = None; break
        if pos is not None:                                    # never hit target: ride to settlement
            win = (side == "yes") == won_yes
            pnl.append((win - pos - bot.fee(1, pos), pos))
    if len(pnl) < 30:
        return None
    x = [p for p, _ in pnl]
    t = statistics.mean(x) / (statistics.stdev(x) / math.sqrt(len(x)))
    return f"{len(x):5d} trades  {sum(p > 0 for p in x) / len(x):4.0%} win  {sum(x) / sum(c for _, c in pnl):+6.1%}/$  t={t:+5.1f}"


train, test = [m for m in markets if m[0] < split], [m for m in markets if m[0] >= split]
print(f"{len(markets)} markets; test from {split[:10]}\n")
print(f"{'dip':>4} {'look':>4} {'take':>4} {'stop':>4} side | {'TRAIN':^42s} | {'TEST (unseen)':^42s}")
for dip, look, take, stop, side in product((0.05, 0.10, 0.20), (3, 5), (0.05, 0.10, 0.20), (0, 0.10), ("yes", "no")):
    a, b = run(dip, look, take, stop, side, train), run(dip, look, take, stop, side, test)
    if a and b:
        print(f"{dip:4.2f} {look:4d} {take:4.2f} {stop:4.2f} {side:4s} | {a} | {b}")
