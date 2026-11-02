#!/usr/bin/env python3
"""Every Kalshi 15-minute series side by side, plus stop-loss / take-profit tests on the maker bot's own trades.
Inputs: maker_cache/<SERIES>_full.json from btc_study.py (full trade tape). Run: .venv/bin/python all15.py

Per series: (1) maker P&L on every favorite-side fill (taker paid 3-20c), (2) BOT SIM = maker.py's exact rules
(one bid per market, 1.5-7.5 min left, favorite 85-96c, hold to settlement), front/back-of-queue, train/test,
(3) exits on those same bot trades, priced off the real later tape:
  stop @X   : once the favorite trades at <= X, sell as TAKER at that print - 1c slippage, paying the taker fee
  tp @X     : a resting sell (maker, no fee) at X fills when a later taker buys our side at >= X
All returns per $ staked; 95% CI from a bootstrap over markets. Markets are one 15-min window each.
"""
import random

import btc_angles as A

random.seed(5)
WIN, BAND = (1.5, 7.5), (0.85, 0.96)          # maker.py: 50-90% of life, favorite 85-96c
fee = lambda p: 0.07 * p * (1 - p)             # taker fee per contract (exact; at 10+ contracts rounding is noise)
EXITS = {"hold": (None, None), "stop .70": (0.70, None), "stop .50": (0.50, None), "stop .30": (0.30, None),
         "stop -15c": ("rel", 0.15), "tp .99": (None, 0.99), "tp .98": (None, 0.98), "tp .99+stop .50": (0.50, 0.99)}
SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M",
          "KXZEC15M", "KXGOLD15M", "KXSILVER15M", "KXPLATINUM15M", "KXPALLADIUM15M", "KXCOPPER15M", "KXWTI15M",
          "KXNATGAS15M", "KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M"]


def bot_trades(markets, back):
    """maker.py's one fill per market: (market, index of the filling trade, our side, our price)."""
    out = []
    for m in markets:
        for j, (side, yp, cnt, t) in enumerate(m["trades"]):
            left = (m["close"] - t) / 60
            if not WIN[0] <= left <= WIN[1] or (back and cnt < 200): continue
            fav = 1 - (yp if side == "yes" else 1 - yp)          # a longshot taker filled our favorite bid
            if BAND[0] <= fav <= BAND[1]:
                out.append((m, j, "no" if side == "yes" else "yes", fav)); break
    return out


def pnl(trade, rule):
    m, j, ours, p = trade
    stop, tp = EXITS[rule]
    level = p - tp if stop == "rel" else stop
    tp = None if stop == "rel" else tp
    for side, yp, cnt, t in m["trades"][j + 1:]:
        sp = yp if ours == "yes" else 1 - yp                    # this print, in our side's price
        if tp and side == ours and sp >= tp:
            return tp - p, "tp"
        if level is not None and sp <= level:
            out = max(sp - 0.01, 0.01)
            return out - fee(out) - p, "stopped"
    return (m["yes"] == (ours == "yes")) - p, "held"


def ret(trades, rule="hold"):
    if not trades: return None
    r = [pnl(t, rule)[0] for t in trades]
    st = sum(t[3] for t in trades)
    boots = sorted(sum(random.choices(r, k=len(r))) / st for _ in range(400))
    return sum(r) / st, boots[10], boots[389], len(r)


fmt = lambda s: f"{s[0]:+6.1%} [{s[1]:+6.1%},{s[2]:+6.1%}] n={s[3]:<4d}" if s else f"{'-':>33s}"


def main():
    rows, pooled, halves = [], {False: [], True: []}, {"ALL train": [], "ALL test (unseen)": []}
    print("=== BOT SIM per series (maker.py rules, hold to settlement) ===")
    print(f"{'series':16s}{'mkts':>5s}{'avg trades':>11s}   {'FRONT all':34s}{'FRONT train':34s}{'FRONT test':34s}{'BACK all':34s}")
    for s in SERIES:
        try:
            data, _ = A.load(s)
        except FileNotFoundError:
            print(f"{s:16s}  (not fetched yet)"); continue
        half = len(data) * 2 // 3
        res = {}
        for back in (False, True):
            tr = bot_trades(data, back)
            pooled[back] += tr
            if not back:
                halves["ALL train"] += [t for t in tr if t[0]["cl"] < half]
                halves["ALL test (unseen)"] += [t for t in tr if t[0]["cl"] >= half]
            res[back] = (ret(tr), ret([t for t in tr if t[0]["cl"] < half]), ret([t for t in tr if t[0]["cl"] >= half]))
        avg = sum(len(m["trades"]) for m in data) / len(data)
        print(f"{s:16s}{len(data):5d}{avg:11,.0f}   {fmt(res[False][0]):34s}{fmt(res[False][1]):34s}{fmt(res[False][2]):34s}"
              f"{fmt(res[True][0]):34s}")
        rows.append((s, data))
    print(f"{'ALL POOLED':16s}{'':16s}   {fmt(ret(pooled[False])):34s}{'':68s}{fmt(ret(pooled[True])):34s}")

    print("\n=== EXITS on the bot's own trades (front-of-queue fills), ret/$ [95% CI] ===")
    print(f"{'series':16s}" + "".join(f"{k:>18s}" for k in EXITS))
    for s, data in rows + [("ALL POOLED", None)] + [(k, k) for k in halves]:
        tr = pooled[False] if data is None else halves[data] if isinstance(data, str) else bot_trades(data, False)
        cells = []
        for k in EXITS:
            v = ret(tr, k)
            cells.append(f"{v[0]:+6.1%}{'*' if v[1] > 0 else '!' if v[2] < 0 else ' '}" if v else "-")
        print(f"{s:16s}" + "".join(f"{c:>18s}" for c in cells))
    print("  (* CI above 0, ! CI below 0)")

    print("\n=== What the stops actually did (all series pooled, front view) ===")
    tr = pooled[False]
    for k in EXITS:
        if k == "hold": continue
        outs = [(pnl(t, k), pnl(t, "hold")[0]) for t in tr]
        stopped = [(a, h) for (a, how), h in outs if how == "stopped"]
        tps = [(a, h) for (a, how), h in outs if how == "tp"]
        whips = sum(h > 0 for _, h in stopped)
        print(f"  {k:16s} stopped {len(stopped):5d} ({len(stopped) / len(tr):5.1%}), of which would have WON if held: "
              f"{whips:4d} ({whips / max(len(stopped), 1):5.1%}); took profit {len(tps):5d}; "
              f"avg stopped exit {sum(a for a, _ in stopped) / max(len(stopped), 1):+.3f} vs hold {sum(h for _, h in stopped) / max(len(stopped), 1):+.3f}")


if __name__ == "__main__":
    main()
