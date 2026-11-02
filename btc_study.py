#!/usr/bin/env python3
"""Bitcoin 15-min only: what does a maker resting a bid on the favorite earn, by price and minutes left?
Bigger + uncut version of maker_pnl.py's KXBTC15M slice (that one: 150 markets, 8k newest trades each).
Run: .venv/bin/python btc_study.py [SERIES]   (default KXBTC15M; resumable; cache maker_cache/<SERIES>_full.json)
     .venv/bin/python btc_study.py [SERIES] --fill-cut   (re-fetch markets that hit the page cap, uncapped)
     --markets=N (default 600) --list-pages=P (default 6; settled-market list pages of 1000, newest first)
     --events=N   sample N settled EVENTS evenly instead, keeping every strike that traded (for KXBTCD etc.,
                  where most strikes never trade and strikes in one event share one outcome)

Maker P&L per contract = price the taker paid - (1 if the taker won). Maker pays no fee. Views as in maker_pnl.py:
front = every trade weighted min(count,5); back = only sweeps >= 200 contracts, weight 5 (pessimistic).
Train = first 2/3 of markets by time, test = last 1/3.
"""
import json, sys

import bot, scan
from backtest import ts

SERIES = next((a for a in sys.argv[1:] if not a.startswith("--")), "KXBTC15M")
opt = lambda name, d: int(next((a.split("=")[1] for a in sys.argv if a.startswith(f"--{name}=")), d))
N_MARKETS, LIST_PAGES, MAX_PAGES, N_EVENTS = opt("markets", 600), opt("list-pages", 6), 40, opt("events", 0)
CACHE = bot.HERE / "maker_cache" / f"{SERIES}_full.json"


def fetch():
    done = json.loads(CACHE.read_text()) if CACHE.exists() else {"markets": None, "out": []}
    if done["markets"] is None and N_EVENTS:
        evs, cursor = [], None
        for _ in range(LIST_PAGES):
            d = scan.get("/events", series_ticker=SERIES, status="settled", with_nested_markets="true", limit=200,
                         **({"cursor": cursor} if cursor else {}))
            evs += [e for e in d["events"] if e.get("markets")]
            cursor = d.get("cursor")
            if not cursor: break
        evs.sort(key=lambda e: min(m["close_time"] for m in e["markets"]))
        evs = evs[::max(1, len(evs) // N_EVENTS)][:N_EVENTS]
        done["markets"] = [{"ticker": m["ticker"], "event": e["event_ticker"], "open": ts(m["open_time"]),
                            "close": ts(m["close_time"]), "yes": m["result"] == "yes"}
                           for e in evs for m in e["markets"]
                           if m.get("result") in ("yes", "no") and float(m.get("volume_fp") or 0) > 0]
        print(f"{len(evs)} events -> {len(done['markets'])} traded markets", flush=True)
    if done["markets"] is None:
        ms, cursor = [], None
        for _ in range(LIST_PAGES):                 # 6 pages ~ 2 months of a 15-min series
            d = scan.get("/markets", series_ticker=SERIES, status="settled", limit=1000, **({"cursor": cursor} if cursor else {}))
            ms += [m for m in d["markets"] if m.get("result") in ("yes", "no")]
            cursor = d.get("cursor")
            if not cursor: break
        ms.sort(key=lambda m: m["close_time"])
        ms = ms[::max(1, len(ms) // N_MARKETS)][:N_MARKETS]   # evenly spread over time
        done["markets"] = [{"ticker": m["ticker"], "event": m["event_ticker"], "open": ts(m["open_time"]), "close": ts(m["close_time"]),
                            "yes": m["result"] == "yes"} for m in ms]
    for m in done["markets"][len(done["out"]):]:
        trades, cut = trades_for(m["ticker"], MAX_PAGES)
        done["out"].append({**m, "trades": trades, "cut": cut})
        if len(done["out"]) % 20 == 0 or len(done["out"]) == len(done["markets"]):
            CACHE.write_text(json.dumps(done))
            print(f"fetched {len(done['out'])}/{len(done['markets'])} markets", flush=True)
    return done["out"]


def trades_for(ticker, pages):
    """Every public trade on a market, newest first; cut=True if we stopped early (page cap or API error)."""
    trades, cursor = [], None
    for _ in range(pages):
        try:
            d = scan.get("/markets/trades", ticker=ticker, limit=1000, **({"cursor": cursor} if cursor else {}))
        except bot.ApiError as e:
            print(f"  {ticker}: {str(e)[-80:]}", flush=True); return trades, True
        trades += [[t["taker_side"], float(t["yes_price_dollars"]), float(t["count_fp"]), ts(t["created_time"])]
                   for t in d["trades"] if not t.get("is_block_trade")]
        cursor = d.get("cursor")
        if not cursor: return trades, False
    return trades, True


def fill_cut():
    done = json.loads(CACHE.read_text())
    todo = [m for m in done["out"] if m["cut"]]
    for i, m in enumerate(todo, 1):
        m["trades"], m["cut"] = trades_for(m["ticker"], 400)
        print(f"re-fetched {i}/{len(todo)} {m['ticker']}: {len(m['trades']):,} trades, cut={m['cut']}", flush=True)
    CACHE.write_text(json.dumps(done))


def table(markets, view, rows, cols, rowkey, colkey):
    acc = {}
    for m in markets:
        for side, yp, cnt, t in m["trades"]:
            paid = yp if side == "yes" else 1 - yp            # what the longshot taker paid
            if not 0.03 <= paid <= 0.20 or (view == "back" and cnt < 200): continue
            w = 5 if view == "back" else min(cnt, 5)
            won = m["yes"] == (side == "yes")
            r, c = rowkey(paid, m, t), colkey(paid, m, t)
            if r is None or c is None: continue
            a = acc.setdefault((r, c), [0.0, 0.0, 0])
            a[0] += w * (paid - won); a[1] += w * (1 - paid); a[2] += 1
    return {k: (a[0] / a[1], a[2]) for k, a in acc.items()}


def bucket(x, cuts):
    return next((f"{a}-{b}" for a, b in cuts if a <= x < b), None)


def main():
    ms = fetch()
    print(f"\n{len(ms)} markets, {sum(len(m['trades']) for m in ms):,} trades, {sum(m['cut'] for m in ms)} hit the page cap")
    split = len(ms) * 2 // 3
    price = [(0.80, 0.85), (0.85, 0.88), (0.88, 0.91), (0.91, 0.94), (0.94, 0.97)]
    left = [(0, 2), (2, 4), (4, 6), (6, 8), (8, 11), (11, 16)]
    fav = lambda paid, m, t: bucket(round(1 - paid, 4), price)
    mins = lambda paid, m, t: bucket((m["close"] - t) / 60, left)
    for view in ("front", "back"):
        for name, part in (("TRAIN", ms[:split]), ("TEST (unseen)", ms[split:])):
            res = table(part, view, price, left, fav, mins)
            print(f"\n== {view}-of-queue, {name}: maker P&L per $ on the favorite, rows = our bid, cols = minutes left ==")
            print(f"{'bid':10s}" + "".join(f"{f'{a}-{b}m':>16s}" for a, b in left))
            for p in price:
                pk = f"{p[0]}-{p[1]}"
                print(f"{pk:10s}" + "".join(f"{res[(pk, f'{a}-{b}')][0]:+7.1%} n={res[(pk, f'{a}-{b}')][1]:<6d}"
                                            if (pk, f"{a}-{b}") in res else f"{'-':>16s}" for a, b in left))


if __name__ == "__main__":
    fill_cut() if "--fill-cut" in sys.argv else main()
