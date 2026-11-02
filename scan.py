#!/usr/bin/env python3
"""Where is Kalshi itself mispriced? Scan fast-settling series for price levels that beat fees.
Run: .venv/bin/python scan.py [top_n_series]      (public data only; cached per series in scan_cache/)

For every settled market: snapshot the real bid/ask at 50% and 85% of its life, then ask "if I had bought
YES at the ask / NO at 1-bid here, holding to settlement, what did I make after that series' taker fee?"
Grouped by series x horizon x price bucket x side.

Honesty rules:
- P&L is summed per EVENT before the t-test: strikes in one event are mutually exclusive, not independent.
- Hundreds of cells get tested, so ~2.5% will show t>2 by luck. A cell only counts if it is found on
  TRAIN (first 2/3 of events) and holds on TEST (last 1/3).
- Only tradeable snapshots: two-sided quote, spread <= 5c, market volume >= 200 contracts.
"""
import json, math, statistics, sys, time
from collections import defaultdict

import bot
from backtest import ts

CACHE_DIR = bot.HERE / "scan_cache"
CACHE_DIR.mkdir(exist_ok=True)
HORIZONS = (0.5, 0.85)
BUCKETS = ((0.03, 0.15), (0.15, 0.30), (0.30, 0.45), (0.45, 0.55), (0.55, 0.70), (0.70, 0.85), (0.85, 0.97))


def get(path, **params):
    time.sleep(0.12)
    return bot.kalshi("GET", path, retries=6, params=params)


def fetch_series(ticker, pages=2):
    f = CACHE_DIR / f"{ticker}.json"
    if f.exists():
        return json.loads(f.read_text())
    markets, cursor = [], None
    for _ in range(pages):
        d = get("/markets", series_ticker=ticker, status="settled", limit=1000, **({"cursor": cursor} if cursor else {}))
        markets += [m for m in d["markets"] if m.get("result") in ("yes", "no") and float(m.get("volume_fp") or 0) >= 200]
        cursor = d.get("cursor")
        if not cursor:
            break
    for m in markets:
        m["_o"], m["_c"] = ts(m["open_time"]), ts(m["close_time"])
        life = m["_c"] - m["_o"]
        m["_iv"] = 1 if life <= 3 * 3600 else 60 if life <= 5 * 86400 else 1440
    out = []
    for iv in (1, 60, 1440):
        group = sorted((m for m in markets if m["_iv"] == iv), key=lambda m: -m["_c"])
        chunks, cur = [], []
        for m in group:     # API cap: tickers x periods-in-range <= 10,000, and <= 100 tickers
            lo, hi = min([x["_o"] for x in cur] + [m["_o"]]), max([x["_c"] for x in cur] + [m["_c"]])
            if cur and (len(cur) == 100 or (len(cur) + 1) * ((hi - lo) / 60 / iv + 2) > 9500):
                chunks.append(cur); cur = []
            cur.append(m)
        if cur:
            chunks.append(cur)
        for chunk in chunks:
            try:
                d = get("/markets/candlesticks", market_tickers=",".join(m["ticker"] for m in chunk), period_interval=iv,
                        start_ts=min(m["_o"] for m in chunk), end_ts=max(m["_c"] for m in chunk))
            except bot.ApiError as e:
                print(f"  {ticker}: chunk skipped ({str(e)[-120:]})", flush=True)
                continue
            candles = {mc["market_ticker"]: mc["candlesticks"] for mc in d["markets"]}
            for m in chunk:
                snaps = []
                for frac in HORIZONS:
                    target = m["_o"] + frac * (m["_c"] - m["_o"])
                    c = next((c for c in candles.get(m["ticker"], []) if c["end_period_ts"] >= target), None)
                    try:
                        snaps.append([frac, float(c["yes_bid"]["close_dollars"]), float(c["yes_ask"]["close_dollars"])])
                    except (TypeError, KeyError, ValueError):
                        pass
                if snaps:
                    out.append({"event": m["event_ticker"], "t": m["_c"], "yes": m["result"] == "yes", "snaps": snaps})
    f.write_text(json.dumps(out))
    return out


def tstat(xs):
    if len(xs) < 20 or statistics.pstdev(xs) == 0:
        return 0.0
    return statistics.mean(xs) / (statistics.stdev(xs) / math.sqrt(len(xs)))


def main():
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    series = [s for s in get("/series", include_volume="true")["series"]
              if s.get("frequency") in ("fifteen_min", "hourly", "daily")]
    series.sort(key=lambda s: -float(s.get("volume_fp") or 0))
    series = series[:top_n]

    cells = []   # one row per series x horizon x bucket x side
    for i, s in enumerate(series):
        try:
            mk = fetch_series(s["ticker"])
        except bot.ApiError as e:
            print(f"[{i + 1}/{len(series)}] {s['ticker']}: FAILED {str(e)[-100:]}", flush=True)
            continue
        print(f"[{i + 1}/{len(series)}] {s['ticker']:22s} {len(mk):5d} markets  fee={s.get('fee_type')} x{s.get('fee_multiplier')}", flush=True)
        if len(mk) < 100:
            continue
        rate = 0.07 * float(s.get("fee_multiplier") or 1)
        events = sorted({m["t"] for m in mk})
        split = events[len(events) * 2 // 3]
        for frac in HORIZONS:
            for lo, hi in BUCKETS:
                for side in ("yes", "no"):
                    per_event = {"train": defaultdict(lambda: [0.0, 0.0]), "test": defaultdict(lambda: [0.0, 0.0])}
                    for m in mk:
                        snap = next((x for x in m["snaps"] if x[0] == frac), None)
                        if not snap or snap[1] <= 0 or snap[2] >= 1 or snap[2] - snap[1] > 0.05:
                            continue
                        p = snap[2] if side == "yes" else 1 - snap[1]      # price we'd pay for this side
                        if not lo <= p < hi:
                            continue
                        # ponytail: fee without per-order cent rounding (right for larger orders, ~0.3%/$ generous at $2)
                        pnl = ((side == "yes") == m["yes"]) - p - rate * p * (1 - p)
                        e = per_event["train" if m["t"] < split else "test"][m["event"]]
                        e[0] += pnl; e[1] += p
                    row = {"series": s["ticker"], "title": s["title"][:34], "frac": frac, "bucket": f"{lo:.2f}-{hi:.2f}", "side": side}
                    for k, ev in per_event.items():
                        staked = sum(v[1] for v in ev.values())
                        row[k] = {"events": len(ev), "ret": sum(v[0] for v in ev.values()) / staked if staked else 0.0,
                                  "t": tstat([v[0] for v in ev.values()])}
                    cells.append(row)

    tested = [c for c in cells if c["train"]["events"] >= 20]
    found = [c for c in tested if c["train"]["t"] > 2 and c["train"]["ret"] > 0]
    held = [c for c in found if c["test"]["t"] > 2 and c["test"]["ret"] > 0]
    print(f"\n{len(tested)} cells tested; {len(found)} look profitable on TRAIN (t>2) "
          f"[~{len(tested) * 0.025:.0f} expected by pure luck]; {len(held)} also hold on TEST (t>2).")
    fmt = lambda c: (f"{c['series']:20s} {c['title']:34s} @{int(c['frac'] * 100):2d}% life  buy {c['side']:3s} priced {c['bucket']}"
                     f" | train {c['train']['ret']:+6.1%}/$ t={c['train']['t']:+4.1f} n={c['train']['events']:4d}"
                     f" | test {c['test']['ret']:+6.1%}/$ t={c['test']['t']:+4.1f} n={c['test']['events']:4d}")
    print("\n== found on train AND confirmed on test ==")
    for c in sorted(held, key=lambda c: -c["test"]["t"]):
        print(fmt(c))
    print("\n== found on train, FAILED on test (what luck looks like) ==")
    for c in sorted((c for c in found if c not in held), key=lambda c: -c["train"]["t"])[:12]:
        print(fmt(c))
    (bot.HERE / "scan_results.json").write_text(json.dumps(cells))


if __name__ == "__main__":
    main()
