#!/usr/bin/env python3
"""Every angle on one question: when does resting a bid on the KXBTC15M favorite make money?
Inputs: maker_cache/KXBTC15M_full.json (btc_study.py) + maker_cache/BTC_1m.json (btc_spot.py).
Run: .venv/bin/python btc_angles.py [SERIES] > out.txt   (default KXBTC15M; KXDOGE15M uses DOGE_1m.json)

A "fill" = a public trade where the taker bought the longshot for 3-20c, so the maker holds the favorite at
80-97c and pays no fee (KXBTC15M fee_type 'quadratic' = taker-only). Maker P&L/contract = paid - (taker won).
Views (as maker_pnl.py): front = every trade, weight min(count,5); back = only sweeps >= 200, weight 5.
Stats are per MARKET (a 15-min window is one unit, its trades are not independent): ret = sum pnl / sum stake,
95% CI from a bootstrap over markets. Train = first 2/3 of markets in time, test = last 1/3, rules picked on train.
Spot features use only the 1-min candle that closed before the trade (no look-ahead).
"""
import bisect, json, math, random, statistics, sys
from collections import defaultdict

import bot

F = bot.HERE / "maker_cache"
SERIES = sys.argv[1] if len(sys.argv) > 1 else "KXBTC15M"
ET = -4 * 3600                    # all data is EDT (Aug-Oct)
LIFE, UNITS_PER_DAY = 15, 96     # set by load()
HOURLY = lambda: LIFE > 20
random.seed(7)


def load(series=None):
    series = series or SERIES
    coin = series[2:].replace("15M", "")
    data = json.loads((F / f"{series}_full.json").read_text())["out"]
    for m in data:
        m["trades"].sort(key=lambda x: x[3])
        m["ts"] = [x[3] for x in m["trades"]]
    data.sort(key=lambda m: m["close"])
    cl = {}
    for m in data:                     # cluster = event (strikes of one event share one outcome); 15-min: 1 per event
        m["cl"] = cl.setdefault(m.get("event", m["ticker"]), len(cl))
    global LIFE, UNITS_PER_DAY
    LIFE = statistics.median((m["close"] - m["open"]) / 60 for m in data)
    UNITS_PER_DAY = 1440 / LIFE        # one event per market-life (15-min: 96/day, hourly: 24/day)
    f = F / f"{coin}_1m.json"
    px = {int(k): v for k, v in json.loads(f.read_text()).items()} if f.exists() else {}
    return data, px


def bot_sim(markets, lo_left, hi_left, lo_fav, hi_fav, back):
    """What maker.py would make: ONE resting bid per market, posted while lo_left..hi_left minutes remain, on the
    favorite at lo_fav..hi_fav, held to settlement. Filled by the first longshot taker in that window paying our
    level (back=True: only a sweep >= 200 contracts counts, i.e. we were at the back of the queue).
    Returns one (pnl per contract, price) per EVENT that filled (first fill across its strikes); None if none."""
    events = defaultdict(list)
    for m in markets: events[m["cl"]].append(m)
    out = []
    for ms in events.values():
        best = None
        for m in ms:
            start = bisect.bisect_left(m["ts"], m["close"] - hi_left * 60)
            for side, yp, cnt, t in m["trades"][start:]:
                left = (m["close"] - t) / 60
                if not lo_left <= left <= hi_left or (back and cnt < 200): continue
                fav = 1 - (yp if side == "yes" else 1 - yp)
                if lo_fav <= fav <= hi_fav:
                    if not best or t < best[0]: best = (t, (m["yes"] != (side == "yes")) - fav, fav)
                    break
        out.append(best[1:] if best else None)
    return out


def agg_stat(per):
    """per = {market: [pnl, stake]} -> (ret per $, lo95, hi95, markets) with a per-market bootstrap."""
    vals = [v for v in per.values() if v[1]]
    if not vals: return None
    boots = []
    for _ in range(300):
        smp = random.choices(vals, k=len(vals))
        boots.append(sum(p for p, _ in smp) / sum(s for _, s in smp))
    boots.sort()
    return sum(p for p, _ in vals) / sum(s for _, s in vals), boots[7], boots[292], len(vals), 0


def price_map(markets, keep_last=None):
    """Maker P&L at EVERY price: bucket by what the taker paid; maker holds the other side. Front view.
    keep_last=N reproduces the old maker_pnl.py cache (only the newest N trades of each market)."""
    paid_b = [(0.01, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.50),
              (0.50, 0.65), (0.65, 0.80), (0.80, 0.90), (0.90, 0.95), (0.95, 0.99)]
    left_b = ([(0, 5), (5, 10), (10, 20), (20, 30), (30, 45), (45, 61)] if HOURLY() else
              [(0, 2), (2, 4), (4, 6), (6, 8), (8, 11), (11, 16)])
    per = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))     # (paid bucket, left bucket) -> market -> sums
    for m in markets:
        mi = m["cl"]
        tr = m["trades"][-keep_last:] if keep_last else m["trades"]
        for side, yp, cnt, t in tr:
            paid = yp if side == "yes" else 1 - yp
            pb = next((b for b in paid_b if b[0] <= paid < b[1]), None)
            lb = next((b for b in left_b if b[0] <= (m["close"] - t) / 60 < b[1]), None)
            if not pb or not lb: continue
            w = min(cnt, 5)
            for key in ((pb, lb), (pb, "all")):
                a = per[key][mi]; a[0] += w * (paid - (m["yes"] == (side == "yes"))); a[1] += w * (1 - paid)
    return paid_b, left_b, per


def show_map(title, markets, keep_last=None):
    paid_b, left_b, per = price_map(markets, keep_last)
    print(f"\n--- {title}")
    print(f"{'taker paid':12s}{'maker holds':12s}{'all mins':>22s}" + "".join(f"{f'{a}-{b}m':>22s}" for a, b in left_b))
    for pb in paid_b:
        cells = [agg_stat(per[(pb, k)]) for k in ["all"] + left_b]
        print(f"{pb[0]:.2f}-{pb[1]:.2f}   {1 - pb[1]:.2f}-{1 - pb[0]:.2f}  " + "".join(
            f"{c[0]:+6.1%} [{c[1]:+5.0%},{c[2]:+5.0%}]{'*' if c[1] > 0 else '!' if c[2] < 0 else ' '}" if c else f"{'-':>22s}"
            for c in cells))


def sim_stat(res):
    got = [r for r in res if r]
    if not got: return None
    rets = [p for p, _ in got]
    staked = sum(f for _, f in got)
    boots = sorted(sum(random.choices(rets, k=len(rets))) / staked for _ in range(500))
    # last field: $/day at 2 contracts, one event per market-life (non-filled events earn 0)
    return (sum(rets) / staked, boots[12], boots[487], len(got), len(got) / len(res),
            sum(rets) / len(res) * 2 * UNITS_PER_DAY)


def fills(data, px):
    out = []
    for m in data:
        mi, tr = m["cl"], m["trades"]
        strike = px.get(m["open"] // 60 * 60 - 60) if "15M" in SERIES else None   # hourly strikes aren't the open
        # suffix max of the price a later taker paid for each side (for the early-exit angle)
        best_yes, best_no = [0.0] * (len(tr) + 1), [0.0] * (len(tr) + 1)
        for j in range(len(tr) - 1, -1, -1):
            side, yp = tr[j][0], tr[j][1]
            best_yes[j] = max(best_yes[j + 1], yp if side == "yes" else 0)
            best_no[j] = max(best_no[j + 1], 1 - yp if side == "no" else 0)
        hist_yes, h = [], 0                                  # (ts, yes price) for Kalshi momentum
        for j, (side, yp, cnt, t) in enumerate(tr):
            hist_yes.append((t, yp))
            paid = yp if side == "yes" else 1 - yp
            if not 0.03 <= paid <= 0.20: continue
            left = (m["close"] - t) / 60
            if left <= 0: continue
            mine = "no" if side == "yes" else "yes"           # maker holds the other side
            sign = 1 if mine == "yes" else -1
            while h < len(hist_yes) and hist_yes[h][0] < t - 120: h += 1
            yp_then = hist_yes[h - 1][1] if h else None
            f = {"mi": mi, "fav": 1 - paid, "pnl": paid - (m["yes"] == (side == "yes")), "stake": 1 - paid,
                 "wf": min(cnt, 5), "back": cnt >= 200, "size": cnt, "left": left,
                 "hour": ((t + ET) % 86400) // 3600, "wkend": ((t + ET) // 86400 + 4) % 7 >= 5,  # 1970-01-01 = Thu
                 "kmove": sign * (yp - yp_then) * 100 if yp_then is not None else None,
                 "later_max": best_yes[j + 1] if mine == "yes" else best_no[j + 1]}
            k = t // 60 * 60 - 60
            hist = [px.get(k - 60 * i) for i in range(11)]
            if strike and None not in hist:
                spot = hist[0]
                sig = statistics.pstdev([math.log(hist[i] / hist[i + 1]) for i in range(10)]) or 1e-5
                f["z"] = sign * (spot - strike) / (sig * spot * math.sqrt(left))
                f["vol"] = sig * 1e4
                f["move3"] = sign * (spot - hist[3]) / spot * 1e4
            out.append(f)
    return out


def stat(rs, view, pnl=lambda r: r["pnl"]):
    """(ret per $, lo95, hi95, markets, fills) with a per-market bootstrap."""
    if view == "back": rs = [r for r in rs if r["back"]]
    w = (lambda r: r["wf"]) if view == "front" else (lambda r: 5)
    per = defaultdict(lambda: [0.0, 0.0])
    for r in rs:
        a = per[r["mi"]]; a[0] += w(r) * pnl(r); a[1] += w(r) * r["stake"]
    if not per: return None
    vals = list(per.values())
    ret = sum(p for p, _ in vals) / sum(s for _, s in vals)
    boots = []
    for _ in range(300):
        smp = random.choices(vals, k=len(vals))
        boots.append(sum(p for p, _ in smp) / sum(s for _, s in smp))
    boots.sort()
    return ret, boots[7], boots[292], len(vals), len(rs)


def fmt(s):
    if not s: return f"{'-':>34s}"
    ret, lo, hi, nm, nf = s
    flag = "*" if lo > 0 else ("!" if hi < 0 else " ")
    return f"{ret:+6.1%} [{lo:+6.1%},{hi:+6.1%}]{flag} m={nm:<4d}"


def show(title, train, test, groups):
    """groups: list of (label, predicate)."""
    for view in ("front", "back"):
        print(f"\n--- {title} | {view}-of-queue | ret/$ [95% CI] (* = CI above 0, ! = below 0), m = markets")
        print(f"{'':22s}{'TRAIN':^42s}{'TEST (unseen)':^42s}")
        for label, pred in groups:
            print(f"{label:22s}{fmt(stat([r for r in train if pred(r)], view)):42s}"
                  f"{fmt(stat([r for r in test if pred(r)], view)):42s}")


def quint(train, feat):
    xs = [r[feat] for r in train if r.get(feat) is not None]
    cuts = statistics.quantiles(xs, n=5)
    edges = [-math.inf] + cuts + [math.inf]
    return [(f"{feat} {edges[i]:.2f}..{edges[i + 1]:.2f}",
             lambda r, lo=edges[i], hi=edges[i + 1]: r.get(feat) is not None and lo <= r[feat] < hi) for i in range(5)]


def main():
    data, px = load()
    rs = fills(data, px)
    split = (data[-1]["cl"] + 1) * 2 // 3                 # by event, in time order
    tr_m, te_m = [m for m in data if m["cl"] < split], [m for m in data if m["cl"] >= split]
    train, test = [r for r in rs if r["mi"] < split], [r for r in rs if r["mi"] >= split]
    days = (data[-1]["close"] - data[0]["open"]) / 86400
    print(f"{SERIES}: {len(data)} markets / {data[-1]["cl"] + 1} events over {days:.0f} days, life {LIFE:.0f} min ({sum(m['cut'] for m in data)} hit the page cap), "
          f"{len(rs):,} favorite-side maker fills ({len(train):,} train / {len(test):,} test)")

    print("\n=== 0. MAP: maker P&L per $ at every price (front view, ALL markets; * CI > 0, ! CI < 0) ===")
    show_map("TRAIN markets", tr_m)
    show_map("TEST markets (unseen)", te_m)
    show_map("DIAGNOSTIC: same markets but only the newest 8,000 trades each (= old maker_pnl.py cache)",
             data, keep_last=8000)
    show_map("DIAGNOSTIC: all trades, same markets", data)

    show("1. overall", train, test, [("all 80-97c", lambda r: True)])
    show("2. our bid price", train, test,
         [(f"bid {a:.2f}-{b:.2f}", lambda r, a=a, b=b: a <= r["fav"] < b)
          for a, b in ((0.80, 0.85), (0.85, 0.88), (0.88, 0.91), (0.91, 0.94), (0.94, 0.97))])
    show("3. minutes left at fill", train, test,
         [(f"left {a}-{b}m", lambda r, a=a, b=b: a <= r["left"] < b)
          for a, b in (((0, 2), (2, 5), (5, 10), (10, 20), (20, 30), (30, 45), (45, 61)) if HOURLY() else
                       ((0, 1), (1, 2), (2, 3), (3, 5), (5, 7), (7, 10), (10, 15)))])
    show("4. time of day (ET)", train, test,
         [(f"{a:02d}-{b:02d} ET", lambda r, a=a, b=b: a <= r["hour"] < b)
          for a, b in ((0, 4), (4, 8), (8, 12), (12, 16), (16, 20), (20, 24))]
         + [("weekend", lambda r: r["wkend"]), ("weekday", lambda r: not r["wkend"])])
    show("5. longshot taker's order size", train, test,
         [(f"size {a}-{b}", lambda r, a=a, b=b: a <= r["size"] < b)
          for a, b in ((0, 10), (10, 50), (50, 200), (200, 1000), (1000, 1e9))])
    for feat, title in (("z", "6. spot distance past strike on our side (std-devs of remaining move)"),
                        ("vol", "7. BTC 1-min volatility, last 10 min (bp)"),
                        ("move3", "8. BTC move last 3 min in our favour (bp)"),
                        ("kmove", "9. Kalshi favorite price change last 2 min (c)")):
        if any(r.get(feat) is not None for r in train):   # spot features exist only for 15-min series
            show(title, train, test, quint(train, feat))

    print("\n=== 10. early exit: also rest a sell at X right after the fill (fills if a later taker pays >= X) ===")
    for view in ("front", "back"):
        for X in (0.97, 0.98, 0.99):
            ex = lambda r, X=X: (X - r["fav"]) if (r["later_max"] >= X and X > r["fav"]) else r["pnl"]
            print(f"  {view:5s} exit@{X:.2f}  train {fmt(stat(train, view, ex))}  test {fmt(stat(test, view, ex))}")

    print("\n=== 11. simple rules, thresholds fixed in advance, judged on TEST ===")
    rules = [("all", lambda r: True),
             ("left >= 2", lambda r: r["left"] >= 2),
             ("left >= 3", lambda r: r["left"] >= 3),
             ("left >= 2, bid >= .88", lambda r: r["left"] >= 2 and r["fav"] >= 0.88),
             ("left >= 2, z >= .5", lambda r: r["left"] >= 2 and r.get("z", -9) >= 0.5),
             ("left >= 2, z .5-2", lambda r: r["left"] >= 2 and 0.5 <= r.get("z", -9) < 2),
             ("left >= 2, move3 >= 0", lambda r: r["left"] >= 2 and r.get("move3", -9) >= 0)]
    for view in ("front", "back"):
        for label, pred in rules:
            tr, te = [r for r in train if pred(r)], [r for r in test if pred(r)]
            print(f"  {view:5s} {label:24s} train {fmt(stat(tr, view))}  test {fmt(stat(te, view))}")

    print("\n=== 12. BOT SIMULATION: one bid per event, held to settlement (what maker.py actually does) ===")
    print(f"    each event = 1 trade; ret/$ [95% CI]; n = events filled; fill%; $/day at 2 contracts x {UNITS_PER_DAY:.0f} events/day")
    BOT_WIN = (round(LIFE * 0.1, 1), round(LIFE * 0.5, 1))      # maker.py quotes in 50-90% of market life
    windows = [(3, 30), (6, 30), (10, 45), (30, 60), (15, 45), (0, 6), (0, 3), (6, 15), (1, 10)] if HOURLY() else [(1.5, 7.5), (3, 7.5), (1.5, 5), (5, 10), (7.5, 14), (3, 12), (0.5, 3), (0, 1.5), (0, 1), (1, 2), (0, 2), (0, 3), (0, 0.5), (0.5, 1)]
    bands = [(0.85, 0.96), (0.80, 0.90), (0.85, 0.92), (0.90, 0.96), (0.93, 0.97), (0.80, 0.97), (0.96, 0.99), (0.50, 0.65), (0.55, 0.70), (0.65, 0.80)]
    for back in (False, True):
        grid = []
        for w in windows:
            for b in bands:
                a, z = sim_stat(bot_sim(tr_m, *w, *b, back)), sim_stat(bot_sim(te_m, *w, *b, back))
                if a and z: grid.append((a[0], w, b, a, z))
        grid.sort(reverse=True)
        print(f"\n  {'back' if back else 'front'}-of-queue; current bot = window {BOT_WIN[0]:g}-{BOT_WIN[1]:g} min left "
              f"(50-90% of life), band 0.85-0.96")
        print(f"  {'window (min left)':18s}{'band':12s}{'TRAIN':^46s}{'TEST (unseen)':^46s}")
        cur = [g for g in grid if g[1] == BOT_WIN and g[2] == (0.85, 0.96)]
        late = [g for g in grid if g[1][1] <= (6 if HOURLY() else 3) and g not in grid[:8]]
        for g in cur + grid[:8] + [None] + late:
            if g is None: print("  -- late windows (last 3 min) --"); continue
            _, w, b, a, z = g
            f = lambda s: f"{s[0]:+6.1%} [{s[1]:+6.1%},{s[2]:+6.1%}] n={s[3]:<3d} {s[4]:4.0%} ${s[5]:+5.2f}/d"
            print(f"  {w[0]:>4}-{w[1]:<12}{b[0]:.2f}-{b[1]:.2f}  {f(a):46s}{f(z):46s}")
        print(f"  (top rows = best on TRAIN; only trust one whose TEST CI is also above 0. "
              f"{len(grid)} combos tried -> expect ~{len(grid) // 20} to look good on train by luck)")


if __name__ == "__main__":
    main()
