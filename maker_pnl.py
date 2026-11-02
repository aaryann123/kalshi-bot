#!/usr/bin/env python3
"""What did MAKERS actually earn on Kalshi? Measured from the public trade tape, not simulated.
Run: .venv/bin/python maker_pnl.py      (public data; cached per series in maker_cache/)

Every trade records which side was the taker. The maker took the other side at the same price, paid no
fee (all fast-settling series are fee_type 'quadratic' = taker-only fees), and held to settlement*.
So maker P&L per contract = price the taker paid - (1 if the taker's side won else 0).
Real fills => adverse selection (getting picked off by informed takers) is already in the numbers.

Two views of "what would a tiny maker have got":
  front-of-queue : every trade, weight min(count, 5) contracts  -> optimistic (always first in line)
  back-of-queue  : only trades >= 200 contracts, weight 5       -> pessimistic (we only fill when someone
                   sweeps the whole level, and big sweeps are the better-informed ones)
Truth for a slow small maker is between them, nearer the pessimistic one.

* Makers can also exit early; hold-to-settlement is the simple, conservative accounting.
P&L is summed per event before the t-test; train = first 2/3 of events, test = last 1/3.
"""
import json, math, statistics
from collections import defaultdict

import bot, scan
from backtest import ts

CACHE_DIR = bot.HERE / "maker_cache"
CACHE_DIR.mkdir(exist_ok=True)
SERIES = ["KXHIGHNY", "KXHIGHLAX", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS", "KXHIGHDEN", "KXRAINNYC",
          "KXBTC15M", "KXETH15M", "KXSOL15M", "KXGOLD15M", "KXWTI15M", "KXBTCD", "KXINXU", "KXWTI", "KXAAAGASD"]
MARKETS_PER_SERIES, MAX_PAGES = 150, 8
# bucket by what the TAKER paid; the maker holds the other side at (1 - that)
BUCKETS = ((0.01, 0.05), (0.05, 0.15), (0.15, 0.30), (0.30, 0.50), (0.50, 0.70), (0.70, 0.85), (0.85, 0.95), (0.95, 1.00))


def fetch(series):
    f = CACHE_DIR / f"{series}.json"
    if f.exists():
        return json.loads(f.read_text())
    markets, cursor = [], None
    for _ in range(2):
        d = scan.get("/markets", series_ticker=series, status="settled", limit=1000, **({"cursor": cursor} if cursor else {}))
        markets += [m for m in d["markets"] if m.get("result") in ("yes", "no") and float(m.get("volume_fp") or 0) >= 200]
        cursor = d.get("cursor")
        if not cursor:
            break
    markets.sort(key=lambda m: m["close_time"])
    markets = markets[::max(1, len(markets) // MARKETS_PER_SERIES)][:MARKETS_PER_SERIES]   # evenly spread over time
    out = []
    for m in markets:
        trades, cursor = [], None
        for _ in range(MAX_PAGES):
            try:
                d = scan.get("/markets/trades", ticker=m["ticker"], limit=1000, **({"cursor": cursor} if cursor else {}))
            except bot.ApiError as e:
                print(f"  {m['ticker']}: {str(e)[-80:]}", flush=True)
                break
            trades += [[t["taker_side"], float(t["yes_price_dollars"]), float(t["count_fp"]), ts(t["created_time"])]
                       for t in d["trades"] if not t.get("is_block_trade")]
            cursor = d.get("cursor")
            if not cursor:
                break
        out.append({"event": m["event_ticker"], "open": ts(m["open_time"]), "close": ts(m["close_time"]),
                    "yes": m["result"] == "yes", "trades": trades})
    f.write_text(json.dumps(out))
    return out


def tstat(xs):
    return statistics.mean(xs) / (statistics.stdev(xs) / math.sqrt(len(xs))) if len(xs) >= 20 and statistics.pstdev(xs) else 0.0


def summarize(markets, view, bucket=None):
    """-> {train/test: (events, maker return per $ staked, t)}"""
    closes = sorted({m["close"] for m in markets})
    split = closes[len(closes) * 2 // 3]
    ev = {"train": defaultdict(lambda: [0.0, 0.0]), "test": defaultdict(lambda: [0.0, 0.0])}
    for m in markets:
        for side, yes_price, count, _ in m["trades"]:
            taker_paid = yes_price if side == "yes" else 1 - yes_price
            if not 0 < taker_paid < 1 or (bucket and not bucket[0] <= taker_paid < bucket[1]):
                continue
            if view == "back" and count < 200:
                continue
            w = 5 if view == "back" else min(count, 5)
            taker_won = (side == "yes") == m["yes"]
            e = ev["train" if m["close"] < split else "test"][m["event"]]
            e[0] += w * (taker_paid - taker_won)      # maker P&L, zero maker fee
            e[1] += w * (1 - taker_paid)              # maker's money at risk
    return {k: (len(v), sum(x[0] for x in v.values()) / max(sum(x[1] for x in v.values()), 1e-9),
                tstat([x[0] for x in v.values()])) for k, v in ev.items()}


def line(label, r):
    return (f"{label:34s} | train {r['train'][1]:+6.1%}/$ t={r['train'][2]:+5.1f} n={r['train'][0]:4d}"
            f" | test {r['test'][1]:+6.1%}/$ t={r['test'][2]:+5.1f} n={r['test'][0]:4d}")


def main():
    data = {}
    for i, s in enumerate(SERIES):
        try:
            data[s] = fetch(s)
        except bot.ApiError as e:
            print(f"[{i + 1}/{len(SERIES)}] {s}: FAILED {str(e)[-100:]}", flush=True)
            continue
        print(f"[{i + 1}/{len(SERIES)}] {s:12s} {len(data[s]):4d} markets  {sum(len(m['trades']) for m in data[s]):8d} trades", flush=True)

    groups = {"WEATHER (6 cities high + NYC rain)": [m for s, v in data.items() if "HIGH" in s or "RAIN" in s for m in v],
              "15-MIN (BTC ETH SOL gold WTI)": [m for s, v in data.items() if s.endswith("15M") for m in v],
              "HOURLY/DAILY financial (BTCD INXU WTI gas)": [m for s, v in data.items() if s in ("KXBTCD", "KXINXU", "KXWTI", "KXAAAGASD") for m in v]}
    for view, title in (("front", "FRONT-OF-QUEUE (optimistic: every trade, <=5 contracts each)"),
                        ("back", "BACK-OF-QUEUE (pessimistic: only fills from sweeps >= 200 contracts)")):
        print(f"\n===== maker return per $ at risk, zero maker fee — {title} =====")
        for g, mk in groups.items():
            if not mk:
                continue
            print(f"\n{g}")
            print(line("  ALL maker fills", summarize(mk, view)))
            for b in BUCKETS:
                r = summarize(mk, view, b)
                if r["train"][0] >= 20:
                    print(line(f"  taker paid {b[0]:.2f}-{b[1]:.2f} (maker holds other side)", r))
        print("\nper series, all fills:")
        for s, mk in data.items():
            print(line(f"  {s}", summarize(mk, view)))


if __name__ == "__main__":
    main()
