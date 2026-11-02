"""Run: .venv/bin/python test_bot.py   (no network, no keys, no pytest needed)"""
import os, tempfile, time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import bot


def market(yes_bid, yes_ask, strike=100.0, mins=8):
    close = datetime.fromtimestamp(time.time() + mins * 60, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"ticker": "T-1", "close_time": close, "floor_strike": strike, "strike_type": "greater_or_equal",
            "yes_bid_dollars": f"{yes_bid:.4f}", "yes_ask_dollars": f"{yes_ask:.4f}",
            "no_ask_dollars": f"{1 - yes_bid:.4f}"}


def test_sizing_never_exceeds_cap():
    for api_balance in (0, 0.5, 3, 10, 5_000, 1e9):
        for price in (0.01, 0.2, 0.5, 0.9, 0.99):
            for open_cost in (0, 2, 8.5, 9.99, 10, 50):
                n = bot.size_order(price, api_balance, open_cost, 0)
                cost = n * price + bot.fee(n, price)
                assert cost <= bot.MAX_TRADE_COST + 1e-9, (api_balance, price, open_cost, n)
                assert cost <= api_balance + 1e-9
                assert n == 0 or open_cost + cost <= bot.ACCOUNT_CAP + 1e-9
    assert bot.size_order(0.5, 5_000, 0, 0) == 3            # $2/trade => 3 @ 0.50 + fee, not 10,000
    assert bot.size_order(0.5, 5_000, 0, bot.MAX_OPEN_POSITIONS) == 0
    for bad in (0, 1, -0.3, 1.5):
        assert bot.size_order(bad, 10, 0, 0) == 0


def test_halts():
    with tempfile.TemporaryDirectory() as d, mock.patch.object(bot, "HALT_FILE", Path(d) / "HALT"):
        assert bot.check_halt(10, 0) is None
        assert "daily loss" in bot.check_halt(7, -3.0)
        assert bot.check_halt(7.01, -2.99) is None
        assert "floor" in bot.check_halt(0.99, 0)
        bot.HALT_FILE.write_text("x")
        assert "HALT file" in bot.check_halt(10, 0)


def test_signal():
    # spot far above strike with 8 min left: YES is near-certain, market at 50c => buy yes
    s = bot.evaluate(market(0.50, 0.51), spot=101.0, ret_5m=0.0, sigma=0.0005)
    assert s["action"] == "buy" and s["side"] == "yes" and s["edge"] > bot.MIN_EDGE, s
    s = bot.evaluate(market(0.50, 0.51), spot=99.0, ret_5m=0.0, sigma=0.0005)
    assert s["action"] == "buy" and s["side"] == "no", s
    assert bot.evaluate(market(0.50, 0.51), 100.0, 0.0, 0.0005)["action"] == "skip"      # no edge
    assert "spread" in bot.evaluate(market(0.40, 0.60), 101.0, 0.0, 0.0005)["reason"]
    assert "min left" in bot.evaluate(market(0.50, 0.51, mins=1), 101.0, 0.0, 0.0005)["reason"]
    assert "MAX_PRICE" in bot.evaluate(market(0.94, 0.95), 110.0, 0.0, 0.0005)["reason"]


def test_losing_streak_halts_trading():
    """End-to-end through cycle(): every trade loses; the bot must stop on its own."""
    with tempfile.TemporaryDirectory() as d, mock.patch.object(bot, "HALT_FILE", Path(d) / "HALT"):
        db = bot.open_db(Path(d) / "j.db")
        n = [0]

        def fake_kalshi(method, path, **kw):
            if path == "/markets":                      # a fresh, badly mispriced market every cycle
                n[0] += 1
                return {"markets": [{**market(0.50, 0.51), "ticker": f"T-{n[0]}"}]}
            return {"market": {"result": "no"}}         # ...that always settles against us

        with mock.patch.object(bot, "kalshi", fake_kalshi), \
             mock.patch.object(bot, "underlying", lambda p: (101.0, 0.0, 0.0005)), \
             mock.patch.object(bot, "SERIES", {"KXBTC15M": "BTC-USD"}), \
             mock.patch.object(bot, "alert", lambda t: None):
            results = [bot.cycle(db, "dry") for _ in range(12)]

        trades = db.execute("SELECT * FROM trades").fetchall()
        lost = -bot.pnl_today(db, "dry")
        assert results[0] is True and results[-1] is False, results
        assert bot.DAILY_LOSS_LIMIT <= lost <= bot.DAILY_LOSS_LIMIT + bot.MAX_OPEN_POSITIONS * bot.MAX_TRADE_COST, lost
        assert len(trades) == 2, len(trades)            # 2 x ~$1.58 loses > $3 => halted before a 3rd
        assert all(t["cost"] <= bot.MAX_TRADE_COST for t in trades)
        before = len(trades)
        bot.cycle(db, "dry")
        assert len(db.execute("SELECT * FROM trades").fetchall()) == before   # stays halted


def test_floor_writes_halt_file():
    with tempfile.TemporaryDirectory() as d, mock.patch.object(bot, "HALT_FILE", Path(d) / "HALT"):
        db = bot.open_db(Path(d) / "j.db")
        alerts = []
        with mock.patch.object(bot, "get_balance", lambda: 0.42), mock.patch.object(bot, "alert", alerts.append), \
             mock.patch.object(bot, "kalshi", lambda *a, **k: 1 / 0):   # any market/order call would blow up
            assert bot.cycle(db, "live") is False
        assert bot.HALT_FILE.exists() and "HARD STOP" in alerts[0]


def test_live_order_refused_without_flag():
    with mock.patch.dict(os.environ, {"LIVE_TRADING": "false"}), mock.patch.object(bot, "kalshi", lambda *a, **k: 1 / 0):
        try:
            bot.place_live_order("T-1", "yes", 1, 0.5)
        except RuntimeError as e:
            assert "LIVE_TRADING" in str(e)
        else:
            raise AssertionError("order went through without LIVE_TRADING=true")
    with mock.patch.dict(os.environ, {"LIVE_TRADING": "true"}), mock.patch.object(bot, "kalshi", lambda *a, **k: 1 / 0):
        try:
            bot.place_live_order("T-1", "yes", 50, 0.5)     # $25 order
        except RuntimeError as e:
            assert "risk limits" in str(e)
        else:
            raise AssertionError("oversized order went through")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
