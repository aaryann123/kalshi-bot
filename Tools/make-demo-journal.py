#!/usr/bin/env python3
"""Write a journal full of SYNTHETIC paper-maker orders and trades, so `maker.py --report` can be tried
without running a bot or touching Kalshi. Random but seeded; nothing here is a real result.

  .venv/bin/python Tools/make-demo-journal.py demo-journal.db
  KALSHI_JOURNAL=demo-journal.db .venv/bin/python maker.py --report
"""
import random, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot, maker

out = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-journal.db")
out.unlink(missing_ok=True)
db = bot.open_db(out)
maker.init_db(db)
rng = random.Random(7)
start = datetime(2026, 1, 5, tzinfo=timezone.utc)

for i in range(240):
    series = rng.choice(["KXDOGE15M", "KXBNB15M"])
    when = start + timedelta(minutes=15 * i)
    event = f"{series}-DEMO{i:04d}"
    ticker = f"{event}-00"
    side, price = rng.choice(["yes", "no"]), round(rng.uniform(0.85, 0.96), 2)
    count = max(1, int(2 / price))
    filled = rng.random() < 0.55
    db.execute("INSERT INTO maker_orders(ts,mode,ticker,event,side,price,count,queue_ahead,placed_at,expire_at,last_check,"
               "close_time,order_id,status,closed_ts,close_reason,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (when.isoformat(), maker.MODE_PAPER, ticker, event, side, price, count, rng.randint(0, 400), time.time(),
                time.time(), time.time(), (when + timedelta(minutes=15)).isoformat(), None,
                "filled" if filled else "canceled", when.isoformat(), "demo", "synthetic demo order"))
    if filled:
        won = rng.random() < price + rng.uniform(-0.03, 0.02)   # roughly fair: no edge baked in
        cost = round(count * price, 2)
        db.execute("INSERT INTO trades(ts,mode,ticker,side,count,price,fee,cost,order_id,reason,status,result,pnl,settled_ts) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (when.isoformat(), maker.MODE_PAPER, ticker, side, count, price, 0.0, cost, None, "synthetic demo fill",
                    "settled", side if won else ("no" if side == "yes" else "yes"), round((count if won else 0) - cost, 2),
                    (when + timedelta(minutes=15)).isoformat()))
db.commit()
print(f"wrote {out}")
