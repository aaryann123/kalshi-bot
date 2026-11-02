#!/usr/bin/env python3
"""Did the model actually know better than the market? Run: .venv/bin/python report.py [dry|live]"""
import sqlite3, sys, time

import bot

mode = sys.argv[1] if len(sys.argv) > 1 else "dry"
db = sqlite3.connect(bot.JOURNAL)

print(f"== trades ({mode}) ==")
n, settled, wins, pnl, staked = db.execute(
    "select count(*), sum(status='settled'), sum(pnl>0), round(sum(pnl),2), round(sum(cost),2) from trades where mode=?", (mode,)).fetchone()
print(f"{n} trades, {settled or 0} settled, {wins or 0} wins, P&L ${pnl or 0:+.2f} on ${staked or 0:.2f} staked")
for day, k, w, p in db.execute("select date(settled_ts,'localtime'), count(*), sum(pnl>0), round(sum(pnl),2) from trades "
                               "where mode=? and status='settled' group by 1", (mode,)):
    print(f"  {day}: {k} trades, {w} wins, ${p:+.2f}")

# Outcomes for every market we evaluated (public endpoint, no keys needed).
res = {}
for (t,) in db.execute("select distinct ticker from signals where mode=? and fair is not null", (mode,)):
    try:
        r = bot.kalshi("GET", f"/markets/{t}")["market"].get("result")
        if r in ("yes", "no"):
            res[t] = r == "yes"
    except bot.ApiError as e:
        print("  skip", t, e)
    time.sleep(0.05)

# ponytail: one row per (market, minute) so duplicate/overlapping bot runs don't double-count
rows = [(f, (yb + ya) / 2, res[t]) for t, f, yb, ya in db.execute(
    "select ticker, fair, yes_bid, yes_ask from signals where mode=? and fair is not null "
    "group by ticker, substr(ts,1,16)", (mode,)) if t in res]
if not rows:
    sys.exit("no settled signals yet")
brier = lambda i: sum((r[i] - r[2]) ** 2 for r in rows) / len(rows)
print(f"\n== calibration: {len(rows)} signals over {len(res)} settled markets ==")
print(f"Brier score (lower is better):  model {brier(0):.4f}   market mid {brier(1):.4f}")
print("model said    n    model  market  actually YES")
for lo, hi in ((0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)):
    b = [r for r in rows if lo <= r[0] < hi]
    if b:
        avg = lambda i: sum(r[i] for r in b) / len(b)
        print(f" {lo:.1f}-{min(hi, 1):.1f}  {len(b):5d}    {avg(0):.2f}   {avg(1):.2f}    {avg(2):.2f}")
big = [r for r in rows if abs(r[0] - r[1]) >= .10]
right = sum((r[0] > r[1]) == r[2] for r in big)
print(f"\nmodel vs market disagreed by >=10c on {len(big)} signals; outcome sided with the model {right} times"
      f" ({right / len(big):.0%})" if big else "\nno >=10c disagreements")
print("\nGo-live bar: model Brier clearly below market's, 'actually YES' tracking the 'model' column in every"
      "\nbucket, and a few hundred settled markets. Anything less is noise.")
