#!/usr/bin/env python3
"""Replay bot.evaluate() over settled Kalshi 15-min markets. Run: .venv/bin/python backtest.py [pages]

One-off calibration check, not a framework. Data: Kalshi settled markets + per-minute quote candles
(public), Coinbase historical 1-min candles (public). Each page = 1000 markets/coin (~10 days).
Downloads are cached in backtest_cache.json; delete it to refetch.

No lookahead: at Kalshi candle end E we use the Coinbase candle that closed at E, and vol/momentum
from candles before it. Still optimistic on fills: assumes we buy at the minute-close ask.
"""
import json, math, statistics, sys
from datetime import datetime, timezone

import bot

CACHE = bot.HERE / "backtest_cache.json"
ts = lambda s: int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()


def fetch(pages):
    data = {}
    for series, product in bot.SERIES.items():
        markets, cursor = [], None
        for _ in range(pages):
            d = bot.kalshi("GET", "/markets", params={"series_ticker": series, "status": "settled", "limit": 1000,
                                                      **({"cursor": cursor} if cursor else {})})
            markets += [m for m in d["markets"] if m.get("result") in ("yes", "no") and m.get("floor_strike")]
            cursor = d.get("cursor")
            if not cursor:
                break
        quotes = {}
        # API cap: tickers x minutes-in-range <= 10,000. Grow each chunk until it would exceed that
        # (gaps in the schedule widen the range).
        markets.sort(key=lambda m: m["close_time"], reverse=True)  # API order isn't chronological
        chunks, cur = [], []
        for m in markets:
            span = (ts((cur or [m])[0]["close_time"]) - ts(m["open_time"])) / 60 + 1
            if cur and (len(cur) + 1) * span > 10000:
                chunks.append(cur); cur = []
            cur.append(m)
        chunks.append(cur)
        for chunk in chunks:
            d = bot.kalshi("GET", "/markets/candlesticks", params={
                "market_tickers": ",".join(m["ticker"] for m in chunk), "period_interval": 1,
                "start_ts": min(ts(m["open_time"]) for m in chunk), "end_ts": max(ts(m["close_time"]) for m in chunk)})
            for mc in d["markets"]:
                quotes[mc["market_ticker"]] = [
                    (c["end_period_ts"], c["yes_bid"]["close_dollars"], c["yes_ask"]["close_dollars"])
                    for c in mc["candlesticks"] if c.get("yes_bid") and c.get("yes_ask")]
        lo, hi = min(ts(m["open_time"]) for m in markets) - 3900, max(ts(m["close_time"]) for m in markets)
        closes = {}  # candle close time -> price
        for start in range(lo, hi, 300 * 60):
            rows = bot.http("GET", f"https://api.exchange.coinbase.com/products/{product}/candles",
                            params={"granularity": 60, "start": iso(start), "end": iso(start + 299 * 60)})
            closes.update({str(r[0] + 60): r[4] for r in rows})
        data[series] = {"closes": closes, "markets": [
            {"ticker": m["ticker"], "close_time": m["close_time"], "floor_strike": m["floor_strike"],
             "strike_type": m.get("strike_type"), "result": m["result"], "quotes": quotes.get(m["ticker"], [])}
            for m in markets]}
        print(f"fetched {series}: {len(markets)} markets, {len(closes)} price minutes", flush=True)
    CACHE.write_text(json.dumps(data))
    return data


def main():
    data = json.loads(CACHE.read_text()) if CACHE.exists() else fetch(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
    rows, trades = [], []   # rows: (model fair, market mid, outcome); trades: (series, pnl, edge)
    for series, d in data.items():
        px = d["closes"]
        for m in d["markets"]:
            won_yes, traded = m["result"] == "yes", False
            for end, yb, ya in m["quotes"]:
                hist = [px.get(str(end - 60 * k)) for k in range(61)]
                if None in hist[:6] or hist.count(None) > 5:
                    continue
                hist = [h for h in hist if h]
                sigma = max(statistics.pstdev(math.log(a / b) for a, b in zip(hist, hist[1:])), 1e-6)
                s = bot.evaluate({**m, "yes_bid_dollars": yb, "yes_ask_dollars": ya,
                                  "no_ask_dollars": str(1 - float(yb))},
                                 hist[0], math.log(hist[0] / hist[5]), sigma, now=end)
                if s["fair"] is None:
                    continue
                rows.append((s["fair"], (float(yb) + float(ya)) / 2, won_yes, series))
                if s["action"] == "buy" and not traded:     # first signal per market, like the bot
                    traded = True
                    n = bot.size_order(s["price"], bot.ACCOUNT_CAP, 0, 0)
                    cost = n * s["price"] + bot.fee(n, s["price"])
                    trades.append((series, (n if (s["side"] == "yes") == won_yes else 0) - cost, s["edge"], cost))

    brier = lambda rs, i: sum((r[i] - r[2]) ** 2 for r in rs) / len(rs)
    nm = sum(len(d["markets"]) for d in data.values())
    print(f"\n== {len(rows)} evaluations over {nm} settled markets ==")
    print(f"Brier (lower is better):  model {brier(rows, 0):.4f}   market mid {brier(rows, 1):.4f}")
    for series in data:
        rs = [r for r in rows if r[3] == series]
        if rs:
            print(f"  {series:10s} model {brier(rs, 0):.4f}   market {brier(rs, 1):.4f}")
    print("\nmodel said      n    model  market  actually YES")
    for lo in (0, .1, .2, .3, .4, .5, .6, .7, .8, .9):
        b = [r for r in rows if lo <= r[0] < lo + .1 + (lo == .9) * .01]
        if b:
            a = lambda i: sum(r[i] for r in b) / len(b)
            print(f" {lo:.1f}-{lo + .1:.1f}  {len(b):7d}    {a(0):.2f}   {a(1):.2f}    {a(2):.2f}")
    if trades:
        pnl = sum(t[1] for t in trades)
        print(f"\n== trades the bot would have taken (no position limit, fills at minute-close ask) ==")
        print(f"{len(trades)} trades, {sum(t[1] > 0 for t in trades)} wins ({sum(t[1] > 0 for t in trades) / len(trades):.0%}), "
              f"P&L ${pnl:+.2f} on ${sum(t[3] for t in trades):.2f} staked ({pnl / sum(t[3] for t in trades):+.1%} per $ staked)")
        for series in data:
            st = [t for t in trades if t[0] == series]
            if st:
                print(f"  {series:10s} {len(st):5d} trades  {sum(t[1] > 0 for t in st) / len(st):4.0%} wins  ${sum(t[1] for t in st):+8.2f}")
        for lo, hi in ((.08, .12), (.12, .2), (.2, 1)):
            st = [t for t in trades if lo <= t[2] < hi]
            if st:
                print(f"  claimed edge {lo:.2f}-{hi:.2f}: {len(st):5d} trades, realized ${sum(t[1] for t in st) / sum(t[3] for t in st):+.3f} per $ staked")


if __name__ == "__main__":
    main()
