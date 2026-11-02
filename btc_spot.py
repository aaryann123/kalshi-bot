#!/usr/bin/env python3
"""Coinbase BTC-USD 1-minute candles covering the btc_study.py markets -> maker_cache/BTC_1m.json {minute_ts: close}.
Historical (start/end) requests, so the live-endpoint lag doesn't matter here. Run: .venv/bin/python btc_spot.py [PRODUCT]   (default BTC-USD -> BTC_1m.json; DOGE-USD -> DOGE_1m.json)
"""
import json, sys, time
from datetime import datetime, timezone

import bot

PRODUCT = sys.argv[1] if len(sys.argv) > 1 else "BTC-USD"
OUT = bot.HERE / "maker_cache" / f"{PRODUCT.split('-')[0]}_1m.json"
URL = f"https://api.exchange.coinbase.com/products/{PRODUCT}/candles"
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()


def main(days=70):
    closes = json.loads(OUT.read_text()) if OUT.exists() else {}
    end = int(time.time()) // 60 * 60
    for start in range(end - days * 86400, end, 300 * 60):       # 300 candles per request
        if str(start) in closes and str(start + 299 * 60) in closes: continue
        for _ in range(3):
            try:
                rows = bot.http("GET", URL, params={"granularity": 60, "start": iso(start), "end": iso(start + 299 * 60)})
                break
            except Exception as e:
                print("retry", e, flush=True); time.sleep(2)
        else:
            continue
        for t, lo, hi, op, cl, vol in rows: closes[str(int(t))] = cl
        time.sleep(0.15)
    OUT.write_text(json.dumps(closes))
    print(f"{len(closes):,} minutes saved, {iso(min(map(int, closes)))} .. {iso(max(map(int, closes)))}")


if __name__ == "__main__":
    main()
