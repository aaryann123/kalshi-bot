#!/usr/bin/env python3
"""Try strategy variants on the backtest cache, with a train/test split so we don't fool ourselves.
Run backtest.py first (it builds backtest_cache.json). Then: .venv/bin/python variants.py

A variant only counts if it's profitable on BOTH the first 2/3 of days (train) and the last 1/3 (test),
with t-stat > 2 on test. t = mean per-trade P&L / its standard error; |t| < 2 is indistinguishable from luck.
Fills assumed at the minute-close ask + taker fee, one trade per market, $2 max per trade, no position limit.
"""
import json, math, statistics
from datetime import datetime

import bot
from backtest import CACHE, ts

data = json.loads(CACHE.read_text())

# One feature row per (market, minute): everything a variant may look at, plus the outcome.
obs = []
for series, d in data.items():
    px = d["closes"]
    for m in d["markets"]:
        close, strike = ts(m["close_time"]), m["floor_strike"]
        for end, yb, ya in m["quotes"]:
            hist = [px.get(str(end - 60 * k)) for k in range(61)]
            if None in hist[:6] or hist.count(None) > 5:
                continue
            hist = [h for h in hist if h]
            yb, ya = float(yb), float(ya)
            if yb <= 0 or ya <= 0 or ya >= 1 or ya - yb > bot.MAX_SPREAD + 1e-9:
                continue
            obs.append({"mkt": m["ticker"], "series": series, "t": close, "mins": (close - end) / 60,
                        "spot": hist[0], "strike": strike, "ret5": math.log(hist[0] / hist[5]),
                        "sigma": max(statistics.pstdev(math.log(a / b) for a, b in zip(hist, hist[1:])), 1e-6),
                        "yb": yb, "ya": ya, "na": round(1 - yb, 4), "yes": m["result"] == "yes"})
obs.sort(key=lambda o: (o["t"], -o["mins"]))
days = sorted({o["t"] // 86400 for o in obs})
split = days[len(days) * 2 // 3] * 86400
print(f"{len(obs)} observations, {len({o['mkt'] for o in obs})} markets, {len(days)} days; "
      f"test = from {datetime.utcfromtimestamp(split):%b %d}\n")


def fair(o, mom=bot.MOMENTUM_WEIGHT, volx=1.0):
    drift = mom * o["ret5"] * min(o["mins"], 5) / 5
    z = (math.log(o["spot"] / o["strike"]) + drift) / (o["sigma"] * volx * math.sqrt(o["mins"]))
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def model(min_edge=bot.MIN_EDGE, lo=2, hi=13, mom=bot.MOMENTUM_WEIGHT, volx=1.0, agree=False, fade=False, max_price=bot.MAX_PRICE):
    def pick(o):
        if not lo <= o["mins"] <= hi:
            return None
        f = fair(o, mom, volx)
        e = {"yes": f - o["ya"] - bot.fee(1, o["ya"]), "no": (1 - f) - o["na"] - bot.fee(1, o["na"])}
        side = max(e, key=e.get)
        if e[side] < min_edge:
            return None
        if agree and (o["ret5"] > 0) != (side == "yes"):     # only trade with the 5-min trend
            return None
        if fade:                                              # bet AGAINST the model's pick
            side = "no" if side == "yes" else "yes"
        return side if o["ya" if side == "yes" else "na"] <= max_price else None
    return pick


def favorite(lo_p, hi_p, lo=2, hi=13):
    """Ignore the model: buy whichever side the market prices in [lo_p, hi_p] (favorite-longshot bias)."""
    def pick(o):
        if not lo <= o["mins"] <= hi:
            return None
        for side, p in (("yes", o["ya"]), ("no", o["na"])):
            if lo_p <= p <= hi_p:
                return side
    return pick


def run(pick, rows):
    pnl, seen = [], set()
    for o in rows:
        if o["mkt"] in seen:
            continue
        side = pick(o)
        if side:
            seen.add(o["mkt"])
            p = o["ya"] if side == "yes" else o["na"]
            n = bot.size_order(p, bot.ACCOUNT_CAP, 0, 0)
            if n:
                pnl.append(((n if (side == "yes") == o["yes"] else 0) - n * p - bot.fee(n, p), n * p))
    if len(pnl) < 30:
        return f"{len(pnl):5d} trades (too few)"
    x = [a for a, _ in pnl]
    t = statistics.mean(x) / (statistics.stdev(x) / math.sqrt(len(x)))
    return f"{len(x):5d} trades  {sum(a > 0 for a in x) / len(x):4.0%} win  {sum(x) / sum(b for _, b in pnl):+6.1%}/$  t={t:+5.1f}"


train, test = [o for o in obs if o["t"] < split], [o for o in obs if o["t"] >= split]
VARIANTS = {
    "baseline (what the bot does)":   model(),
    "last 5 min only":                model(lo=2, hi=5),
    "last 5 min, allow price<=0.97":  model(lo=2, hi=5, max_price=0.97),
    "early only (8-13 min)":          model(lo=8, hi=13),
    "momentum off":                   model(mom=0),
    "momentum full (1.0)":            model(mom=1.0),
    "vol x1.3 (less confident)":      model(volx=1.3),
    "vol x1.3, momentum off":         model(volx=1.3, mom=0),
    "edge >= 0.15":                   model(min_edge=0.15),
    "edge >= 0.25":                   model(min_edge=0.25),
    "model + trend must agree":       model(agree=True),
    "agree + last 5 min":             model(agree=True, lo=2, hi=5),
    "FADE the model":                 model(fade=True),
    "FADE the model, edge >= 0.15":   model(fade=True, min_edge=0.15),
    "buy favorites 0.80-0.90":        favorite(0.80, 0.90),
    "buy favorites 0.90-0.97":        favorite(0.90, 0.97),
    "buy favorites 0.90-0.97, <5min": favorite(0.90, 0.97, hi=5),
    "buy longshots 0.03-0.15":        favorite(0.03, 0.15),
    "coin flip: buy YES 0.45-0.55":   lambda o: "yes" if 0.45 <= o["ya"] <= 0.55 and o["mins"] >= 10 else None,
}
print(f"{'variant':32s} | {'TRAIN (first 2/3)':^46s} | {'TEST (last 1/3, unseen)':^46s}")
for name, pick in VARIANTS.items():
    print(f"{name:32s} | {run(pick, train)} | {run(pick, test)}")
