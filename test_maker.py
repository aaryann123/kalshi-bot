"""Run: .venv/bin/python test_maker.py   (no network, no keys)"""
import os, tempfile, time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import bot, maker

iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def market(yes_bid, yes_ask, frac=0.7, ticker="S-EV1-1", event="S-EV1", bid_size=100, ask_size=100):
    now = time.time()
    return {"ticker": ticker, "event_ticker": event, "open_time": iso(now - 900 * frac), "close_time": iso(now + 900 * (1 - frac)),
            "yes_bid_dollars": f"{yes_bid:.4f}", "yes_ask_dollars": f"{yes_ask:.4f}", "volume_fp": "5000",
            "yes_bid_size_fp": str(bid_size), "yes_ask_size_fp": str(ask_size)}


def trade(side, yes_price, count, age=0.0):
    return {"taker_side": side, "yes_price_dollars": f"{yes_price:.4f}", "no_price_dollars": f"{1 - yes_price:.4f}",
            "count_fp": str(count), "created_time": iso(time.time() - age)}


def test_plan_only_ever_bids_the_favorite():
    w, _ = maker.plan(market(0.90, 0.91, bid_size=250))
    assert w["side"] == "yes" and w["price"] == 0.90 and w["queue_ahead"] == 250
    w, _ = maker.plan(market(0.08, 0.09, ask_size=40))           # NO is the favorite: NO bid = 1 - 0.09
    assert w["side"] == "no" and abs(w["price"] - 0.91) < 1e-9 and w["queue_ahead"] == 40
    for yb, ya in ((0.50, 0.51), (0.80, 0.82), (0.17, 0.19), (0.98, 0.99), (0.005, 0.015)):
        assert maker.plan(market(yb, ya))[0] is None, (yb, ya)   # no favorite in [0.85, 0.96] -> no order
    for yb in [x / 100 for x in range(1, 99)]:                  # sweep: never a bid on the longshot side
        w, _ = maker.plan(market(yb, yb + 0.01))
        assert w is None or maker.MIN_FAV <= w["price"] <= maker.MAX_FAV
    assert maker.plan(market(0.90, 0.91, frac=0.3))[0] is None   # too early
    assert maker.plan(market(0.90, 0.91, frac=0.95))[0] is None  # too late
    assert maker.plan(market(0.91, 0.90))[0] is None             # crossed book
    with mock.patch.object(maker, "MAX_LIFE_H", 0.1):               # 15-min market > 0.1h: skipped
        assert maker.plan(market(0.90, 0.91))[0] is None


def test_paper_fill_is_conservative():
    o = {"side": "yes", "price": 0.90, "count": 2, "queue_ahead": 100, "traded_at_price": 0.0, "last_check": time.time() - 60}
    assert maker.paper_fill(o, [trade("no", 0.90, 50)])[0] is False            # 50 < 100 ahead of us
    assert maker.paper_fill(o, [trade("no", 0.90, 50), trade("no", 0.90, 52)])[0] is True   # queue + ours consumed
    assert maker.paper_fill(o, [trade("no", 0.89, 1)])[0] is True              # paid 11c > our 10c level: swept through
    assert maker.paper_fill(o, [trade("no", 0.91, 9999)])[0] is False          # better bids than ours took it
    assert maker.paper_fill(o, [trade("yes", 0.90, 9999)])[0] is False         # favorite BUYERS never fill our bid
    assert maker.paper_fill(o, [trade("no", 0.89, 1, age=120)])[0] is False    # before we were resting
    # running total persists across cycles without double counting
    t1 = trade("no", 0.90, 60, age=30)
    _, seen, last = maker.paper_fill(o, [t1])
    o2 = {**o, "traded_at_price": seen, "last_check": last}
    assert maker.paper_fill(o2, [t1])[1] == 60                                  # same trade again: ignored
    assert maker.paper_fill(o2, [t1, trade("no", 0.90, 45)])[0] is True
    no = {**o, "side": "no"}                                                    # favorite NO @0.90: yes-buyers paying >=10c
    assert maker.paper_fill(no, [trade("yes", 0.11, 1)])[0] is True
    assert maker.paper_fill(no, [trade("yes", 0.09, 9999)])[0] is False


def test_live_place_refuses():
    boom = lambda *a, **k: 1 / 0
    with mock.patch.object(maker, "kalshi", boom):
        for env, args, msg in (({"LIVE_TRADING": "false"}, ("T", "yes", 2, 0.90, 0), "LIVE_TRADING"),
                               ({"LIVE_TRADING": "true"}, ("T", "yes", 50, 0.90, 0), "risk"),      # $45
                               ({"LIVE_TRADING": "true"}, ("T", "yes", 2, 0.10, 0), "risk")):      # longshot side
            with mock.patch.dict(os.environ, env):
                try:
                    maker.live_place(*args)
                except RuntimeError as e:
                    assert msg in str(e)
                else:
                    raise AssertionError(f"order went through: {args}")


def test_cycle_slots_cap_fill_settle_and_halt():
    with tempfile.TemporaryDirectory() as d, mock.patch.object(bot, "HALT_FILE", Path(d) / "HALT"), \
         mock.patch.object(maker, "SERIES", ["S"]), mock.patch.object(bot, "alert", lambda t: None), \
         mock.patch.object(maker, "PAPER_DAILY_LIMIT", True):                  # exercise the live-style halt
        db = bot.open_db(Path(d) / "j.db"); maker.init_db(db)
        mkts = [market(0.90, 0.91, ticker=f"S-EV{i}-1", event=f"S-EV{i}") for i in range(8)]
        mkts.append(market(0.90, 0.91, ticker="S-EV0-2", event="S-EV0"))       # second strike of the same event
        tape, results = [], {}

        def fake(method, path, **kw):
            if path == "/markets":
                return {"markets": mkts}
            if path == "/markets/trades":
                return {"trades": tape}
            return {"market": {"result": results.get(path.rsplit("/", 1)[1], "")}}

        with mock.patch.object(maker, "kalshi", fake), mock.patch.object(bot, "kalshi", fake):
            assert maker.cycle(db, maker.MODE_PAPER) is True
            rest = db.execute("SELECT * FROM maker_orders WHERE status='resting'").fetchall()
            assert len(rest) == maker.MAX_SLOTS and len({o["event"] for o in rest}) == maker.MAX_SLOTS
            assert sum(o["count"] * o["price"] for o in rest) <= bot.ACCOUNT_CAP
            assert all(o["count"] * o["price"] <= bot.MAX_TRADE_COST for o in rest)
            maker.cycle(db, maker.MODE_PAPER)
            assert db.execute("SELECT count(*) FROM maker_orders").fetchone()[0] == maker.MAX_SLOTS   # no stacking

            tape.append(trade("no", 0.85, 5))                                   # a sweep through every level
            maker.cycle(db, maker.MODE_PAPER)
            trades = db.execute("SELECT * FROM trades").fetchall()
            assert len(trades) == maker.MAX_SLOTS and all(t["fee"] == 0 and t["side"] == "yes" for t in trades)
            assert db.execute("SELECT count(*) FROM maker_orders WHERE status='resting'").fetchone()[0] == 0  # slots full

            tape.clear()
            for t in trades:
                results[t["ticker"]] = "no"                                     # every longshot comes in
            assert maker.cycle(db, maker.MODE_PAPER) is False                   # 3 x -$1.80 > $3 daily limit
            assert bot.pnl_today(db, maker.MODE_PAPER) <= -bot.DAILY_LOSS_LIMIT
            n = db.execute("SELECT count(*) FROM maker_orders").fetchone()[0]
            maker.cycle(db, maker.MODE_PAPER)
            assert db.execute("SELECT count(*) FROM maker_orders").fetchone()[0] == n   # stays halted, rests nothing


def test_paper_ignores_daily_limit_but_not_the_floor():
    with tempfile.TemporaryDirectory() as d, mock.patch.object(bot, "HALT_FILE", Path(d) / "HALT"), \
         mock.patch.object(maker, "SERIES", ["S"]), mock.patch.object(bot, "alert", lambda t: None):
        db = bot.open_db(Path(d) / "j.db"); maker.init_db(db)
        lose = lambda pnl: db.execute("INSERT INTO trades(ts,mode,ticker,side,count,price,fee,cost,status,result,pnl,settled_ts) "
                                      "VALUES(datetime('now'),?,'S-X-1','yes',2,0.9,0,1.8,'settled','no',?,datetime('now'))",
                                      (maker.MODE_PAPER, pnl))
        lose(-4.0); db.commit()                                                 # past the $3 daily limit
        fake = lambda method, path, **kw: {"markets": []}
        with mock.patch.object(maker, "kalshi", fake), mock.patch.object(bot, "kalshi", fake):
            assert maker.cycle(db, maker.MODE_PAPER) is True                    # paper keeps trading
            with mock.patch.object(maker, "PAPER_DAILY_LIMIT", True):
                assert maker.cycle(db, maker.MODE_PAPER) is False               # the switch still works
            lose(-5.5); db.commit()                                             # equity 10 - 9.5 < $1 floor
            assert maker.cycle(db, maker.MODE_PAPER) is False and bot.HALT_FILE.exists()


def test_several_strikes_per_event_never_the_same_strike_twice():
    with tempfile.TemporaryDirectory() as d, mock.patch.object(bot, "HALT_FILE", Path(d) / "HALT"), \
         mock.patch.object(maker, "SERIES", ["S"]), mock.patch.object(maker, "MAX_PER_EVENT", 3), \
         mock.patch.object(maker, "MAX_SLOTS", 10):
        db = bot.open_db(Path(d) / "j.db"); maker.init_db(db)
        mkts = [market(0.90, 0.91, ticker=f"S-A-{i}", event="S-A") for i in range(5)] + [market(0.90, 0.91, ticker="S-B-1", event="S-B")]
        fake = lambda method, path, **kw: {"markets": mkts} if path == "/markets" else {"trades": []}
        with mock.patch.object(maker, "kalshi", fake), mock.patch.object(bot, "kalshi", fake):
            for _ in range(2):                                                  # second cycle must not stack
                maker.cycle(db, maker.MODE_PAPER)
            rest = db.execute("SELECT * FROM maker_orders WHERE status='resting'").fetchall()
            assert sorted(o["event"] for o in rest) == ["S-A"] * 3 + ["S-B"]
            assert len({o["ticker"] for o in rest}) == len(rest)


def test_paper_profile_is_paper_only():
    saved = bot.ACCOUNT_CAP, bot.MAX_TRADE_COST, bot.DAILY_LOSS_LIMIT, bot.BALANCE_FLOOR, bot.HALT_FILE
    try:
        try:
            maker.apply_profile("100", live=True)
            assert False, "a size profile must never apply to live trading"
        except SystemExit:
            pass
        assert bot.ACCOUNT_CAP == saved[0]                                      # refused before touching caps
        assert maker.apply_profile("100", live=False) == "paper-maker-100"
        assert (bot.ACCOUNT_CAP, bot.MAX_TRADE_COST, bot.DAILY_LOSS_LIMIT) == (100.0, 10.0, 20.0)
        assert bot.HALT_FILE.name == "HALT-100"                                 # can't halt the $10 bot
    finally:
        bot.ACCOUNT_CAP, bot.MAX_TRADE_COST, bot.DAILY_LOSS_LIMIT, bot.BALANCE_FLOOR, bot.HALT_FILE = saved


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
