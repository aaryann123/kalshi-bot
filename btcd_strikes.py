#!/usr/bin/env python3
"""Hourly BTC (KXBTCD): rest a bid on EVERY strike whose favorite is in band, not just one per event.
No look-ahead: each strike's first in-band fill, then per event keep the EARLIEST K fills in time (Kalshi lists
strikes ascending and the lowest always settle YES, so picking strikes by list order would peek at the outcome).
CI = bootstrap over events (strikes of one hour share an outcome). Run: .venv/bin/python btcd_strikes.py
"""
import sys, random
from collections import defaultdict
sys.argv = ["x", "KXBTCD"]
import btc_angles as A
random.seed(9)
data, _ = A.load("KXBTCD")
split = (data[-1]["cl"] + 1) * 2 // 3
def sim(ms, win, band, back, K):
    per = defaultdict(list)
    for m in ms:
        for side, yp, cnt, t in m["trades"]:
            left = (m["close"] - t) / 60
            if not win[0] <= left <= win[1] or (back and cnt < 200): continue
            fav = 1 - (yp if side == "yes" else 1 - yp)
            if band[0] <= fav <= band[1]:
                per[m["cl"]].append((t, (m["yes"] != (side == "yes")) - fav, fav)); break
    ev = [sorted(v)[:K] for v in per.values() if v]
    P = [sum(p for _, p, _ in v) for v in ev]; S = [sum(s for _, _, s in v) for v in ev]
    wins = sum(p > 0 for v in ev for _, p, _ in v); n = sum(len(v) for v in ev)
    idx = range(len(ev))
    boots = sorted(sum(P[i] for i in b) / sum(S[i] for i in b) for b in (random.choices(idx, k=len(ev)) for _ in range(500)))
    n_ev = len({m["cl"] for m in ms})
    return sum(P) / sum(S), boots[12], boots[487], len(ev), n / len(ev), sum(P) / n_ev * 24, wins / n, sum(S) / n
f = lambda s: f"{s[0]:+6.1%} [{s[1]:+6.1%},{s[2]:+6.1%}] fills/ev={s[4]:4.1f} won {s[6]:.1%} vs needed {s[7]:.1%} $/d@1c={s[5]:+5.2f}"
tr, te = [m for m in data if m["cl"] < split], [m for m in data if m["cl"] >= split]
for win in ((6, 30), (3, 57)):
    for band in ((0.85, 0.96), (0.80, 0.97)):
        for K in (1, 3, 99):
            for back in (False, True):
                print(f"win {win} band {band} first {K:2d}/event {'back ' if back else 'front'} | train {f(sim(tr, win, band, back, K))} | test {f(sim(te, win, band, back, K))}")
print("\nALL 300 events pooled:")
for win, band, K in (((3, 57), (0.85, 0.96), 99), ((3, 57), (0.85, 0.96), 3), ((6, 30), (0.85, 0.96), 1)):
    for back in (False, True):
        print(f"win {win} band {band} first {K:2d}/event {'back ' if back else 'front'} | {f(sim(data, win, band, back, K))}")
