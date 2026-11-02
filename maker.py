#!/usr/bin/env python3
"""Kalshi maker bot v2: rest post-only bids on the FAVORITE side (= sell overpriced longshots). See README.

Paper mode (default) places nothing: it logs the order it would have rested, then watches the public
trade tape to decide, conservatively, whether that order would have filled. LIVE_TRADING=true rests
real post_only orders. Same $10 cap, halts, journal and single-instance lock as bot.py.

Usage:
  python maker.py --once     # one cycle
  python maker.py            # loop forever (Ctrl-C to stop)
  python maker.py --report   # fill rate + P&L so far (every bot that has written to the journal)
  PAPER_PROFILE=100 python maker.py   # a second, bigger PAPER bot alongside the $10 one (never live)
  PAPER_PROFILE=btcd3 python maker.py # paper: hourly BTC, up to 3 strikes per hour, $100 caps

Why these rules (maker_pnl.py, 4.2M real trades): makers pay no fee on these series; selling longshots
(taker pays 1-15c) earned +1-6%/$ across every group, both queue views, train and test. Being the maker on
the LONGSHOT side lost 30-96%/$, so this bot never rests an order below MIN_FAV. In 15-min markets the edge
existed only during 50-90% of a market's life.
"""
import argparse, logging, os, sys, time, uuid
from collections import Counter
from datetime import datetime

import bot
from bot import ApiError, kalshi, now_iso

# ---- Maker parameters -------------------------------------------------------
SERIES = ["KXDOGE15M", "KXBNB15M"]  # 2026-10-09, all15.py (19 series, full tape): with these exact rules DOGE is
                        # +1.3%/$ front-of-queue, +5.2% [+1.4,+8.2] back; BNB +6.8% [+3.7,+9.6] front (both halves +),
                        # thin book so fills are the open question. BTC15M is -3.1%: its old +5% was an artifact of
                        # maker_pnl.py's 8k-trade cache. Everything else ~0. --report shows each series separately.
MIN_FAV, MAX_FAV = 0.85, 0.96   # rest bids on the favorite only in this range (taker pays 4-15c for the longshot).
                                # Above 0.96 you risk 97c+ to make <=3c: study says still +, but one loss = 30+ wins.
LIFE_LO, LIFE_HI = 0.50, 0.90   # fraction of market life in which we quote
MAX_SLOTS = 5                   # resting + filled, combined. Strict: even if every resting order fills we hold <= MAX_SLOTS.
MAX_PER_EVENT = 1               # orders per event; strikes within an event are (nearly) the same bet
MAX_LIFE_H = None               # skip markets that live longer than this many hours (None = no limit)
PAPER_DAILY_LIMIT = False       # paper bots run through losing days to collect data; live always enforces it
REPEG = 0.02                    # cancel if the favorite's best bid drifts this far from our price
POLL_SECONDS = 20

MODE_PAPER, MODE_LIVE = "paper-maker", "live-maker"
# Paper-only profiles: caps = (ACCOUNT_CAP, MAX_TRADE_COST, DAILY_LOSS_LIMIT, BALANCE_FLOOR), plus optional overrides of
# this module's settings. Own journal mode, lock, log and HALT file, so it runs next to the $10 bot. Refused with
# LIVE_TRADING=true: live caps are edited in bot.py.
PROFILES = {
    "100": {"caps": (100.00, 10.00, 20.00, 10.00)},          # ~10-11 contracts/trade, halt after a $20 day
    # Hourly BTC (btcd_strikes.py, 300 events): bids on the first 3 strikes per hour whose favorite is 85-96c, 3-57
    # min left: +2.4%/$ [+0.4,+4.2] front-of-queue. Strikes of one hour share an outcome: on 2026-10-09 a +$684 hour
    # took 6 strikes at once (-$57 at 12/hour), so capped at 3 (worst hour ~-$28). The old 12/hour run is mode
    # paper-maker-btcd in the journal.
    "btcd3": {"caps": (100.00, 10.00, 20.00, 10.00), "SERIES": ["KXBTCD"], "MAX_PER_EVENT": 3, "MAX_SLOTS": 6,
              "LIFE_LO": 0.05, "LIFE_HI": 0.95, "MAX_LIFE_H": 1.5},  # hourly only: the study was 288/300 hourly
}
log = logging.getLogger("maker")
ts = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def init_db(db):
    db.execute("""CREATE TABLE IF NOT EXISTS maker_orders(id INTEGER PRIMARY KEY, ts TEXT, mode TEXT, ticker TEXT,
        event TEXT, side TEXT, price REAL, count INTEGER, queue_ahead REAL, traded_at_price REAL DEFAULT 0,
        placed_at REAL, last_check REAL, expire_at REAL, close_time TEXT, order_id TEXT, status TEXT DEFAULT 'resting',
        closed_ts TEXT, close_reason TEXT, reason TEXT)""")
    db.commit()


def plan(m, now=None):
    """Pure decision for one market: the order we'd want resting, or (None, why not).
    Returns ({side, price, queue_ahead, expire_at}, reason)."""
    now = now or time.time()
    try:
        o, c = ts(m["open_time"]), ts(m["close_time"])
        yb, ya = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
        yb_size, ya_size = float(m.get("yes_bid_size_fp") or 0), float(m.get("yes_ask_size_fp") or 0)
    except (KeyError, ValueError, TypeError):
        return None, "bad market data"
    if MAX_LIFE_H and c - o > MAX_LIFE_H * 3600:
        return None, f"market lives {(c - o) / 3600:.0f}h, over {MAX_LIFE_H}h"
    frac = (now - o) / max(c - o, 1)
    if not LIFE_LO <= frac <= LIFE_HI:
        return None, f"{frac:.0%} of life, outside {LIFE_LO:.0%}-{LIFE_HI:.0%}"
    if not 0 < yb < ya < 1:
        return None, "one-sided or crossed book"
    # Favorite = the side whose BID is high. NO's bid is 1 - yes_ask, and the queue there is the yes-ask size.
    for side, bid, ask, queue in (("yes", yb, ya, yb_size), ("no", round(1 - ya, 4), round(1 - yb, 4), ya_size)):
        if MIN_FAV <= bid <= MAX_FAV:
            why = (f"{side.upper()} is the favorite: bid {bid:.3f} / ask {ask:.3f}, {frac:.0%} of life. Join the bid at "
                   f"{bid:.3f} (post-only; a longshot buyer paying {1 - bid:.3f} fills us), {queue:.0f} contracts ahead")
            return {"side": side, "price": bid, "queue_ahead": queue, "expire_at": o + LIFE_HI * (c - o)}, why
    return None, f"no favorite in range (yes bid {yb:.2f}, no bid {1 - ya:.2f})"


def paper_fill(order, trades):
    """Would this resting bid have filled, given the taker trades since it was placed? Conservative:
    - a longshot buyer paying MORE than (1 - our price) swept through our level  -> filled for sure
    - paying exactly (1 - our price): we fill only after everyone who was ahead of us at placement has
      (assumes nobody ahead ever cancels, so real fills come sooner than this says)
    Only trades newer than order['last_check'] are counted, so calling this every cycle never double-counts.
    Returns (filled, traded_at_price_total, new_last_check)."""
    longshot = "no" if order["side"] == "yes" else "yes"
    ours = round(1 - order["price"], 4)                  # what the longshot buyer pays at our level
    at_price, last = order["traded_at_price"], order["last_check"]
    for t in sorted(trades, key=lambda t: t["created_time"]):
        when = ts(t["created_time"])
        if when <= order["last_check"]:
            continue
        last = max(last, when)
        if t["taker_side"] != longshot:
            continue
        paid = round(float(t["yes_price_dollars" if longshot == "yes" else "no_price_dollars"]), 4)
        if paid > ours + 1e-9:
            return True, at_price, last
        if abs(paid - ours) <= 1e-9:
            at_price += float(t["count_fp"])
            if at_price >= order["queue_ahead"] + order["count"]:
                return True, at_price, last
    return False, at_price, last


def tape_since(ticker, since):
    """All public trades on a market after `since` (busy 15-min markets print >1000 trades between polls)."""
    trades, cursor = [], None
    for _ in range(10):
        d = kalshi("GET", "/markets/trades", params={"ticker": ticker, "limit": 1000, "min_ts": int(since),
                                                     **({"cursor": cursor} if cursor else {})})
        trades += d["trades"]
        cursor = d.get("cursor")
        if not cursor:
            break
    return trades


# ---- Live order plumbing (only reachable with LIVE_TRADING=true) ------------
def live_place(ticker, side, count, price, expire_at):
    if os.environ.get("LIVE_TRADING") != "true":
        raise RuntimeError("live order attempted without LIVE_TRADING=true")
    if count * price > min(bot.MAX_TRADE_COST, bot.ACCOUNT_CAP) + 1e-9 or price < MIN_FAV:
        raise RuntimeError("order outside risk limits")
    body = {"ticker": ticker, "client_order_id": str(uuid.uuid4()),
            "side": "bid" if side == "yes" else "ask",                       # V2 quotes from the YES side:
            "price": f"{price if side == 'yes' else 1 - price:.4f}",          # buy NO at p == sell YES at 1-p
            "count": f"{count}.00", "time_in_force": "good_till_canceled",
            "expiration_time": int(expire_at),      # server-side expiry: the order dies on its own if we crash
            "post_only": True, "cancel_order_on_pause": True, "self_trade_prevention_type": "maker"}
    return kalshi("POST", "/portfolio/events/orders", auth=True, retries=1, json=body)["order_id"]


def live_status(order_id):
    o = kalshi("GET", f"/portfolio/orders/{order_id}", auth=True)["order"]
    return o["status"], int(float(o.get("fill_count_fp") or 0))


def live_cancel(order_id):
    try:
        kalshi("DELETE", f"/portfolio/events/orders/{order_id}", auth=True, retries=1)
    except ApiError as e:
        log.warning("cancel %s: %s (already gone?)", order_id, e)


# ---- Main cycle -------------------------------------------------------------
def close_order(db, o, status, why):
    db.execute("UPDATE maker_orders SET status=?, closed_ts=?, close_reason=? WHERE id=?", (status, now_iso(), why, o["id"]))
    db.commit()


def record_fill(db, mode, o, count):
    cost = round(count * o["price"], 2)                 # maker fee is zero on these series
    db.execute("INSERT INTO trades(ts,mode,ticker,side,count,price,fee,cost,order_id,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (now_iso(), mode, o["ticker"], o["side"], count, o["price"], 0.0, cost, o["order_id"], o["reason"]))
    close_order(db, o, "filled", f"{count} filled")
    log.info("FILL  [%s] %s bought %s x%d @ %.3f as maker, cost %.2f (wins %.2f if %s)", mode, o["ticker"], o["side"],
             count, o["price"], cost, count - cost, o["side"].upper())


def cycle(db, mode):
    bot.settle(db, mode)
    live = mode == MODE_LIVE
    resting = db.execute("SELECT * FROM maker_orders WHERE mode=? AND status='resting'", (mode,)).fetchall()

    # 1. Resolve resting orders: filled? expired? market moved away?
    markets = {}
    for series in SERIES:
        try:
            cursor = None
            for _ in range(5):                       # hourly BTC lists ~190 strikes per open hour
                d = kalshi("GET", "/markets", params={"series_ticker": series, "status": "open", "limit": 200,
                                                      **({"cursor": cursor} if cursor else {})})
                markets.update((m["ticker"], m) for m in d["markets"])
                cursor = d.get("cursor")
                if not cursor: break
        except ApiError as e:
            log.error("%s: %s", series, e)
    for o in resting:
        try:
            if live:
                status, filled = live_status(o["order_id"])
                if filled:
                    record_fill(db, mode, o, filled)
                    if status == "resting":
                        live_cancel(o["order_id"])          # partial fill: take it, pull the rest
                    continue
                if status != "resting":
                    close_order(db, o, "canceled", f"exchange says {status}")
                    continue
            else:
                filled, seen, last = paper_fill(o, tape_since(o["ticker"], o["last_check"]))
                db.execute("UPDATE maker_orders SET traded_at_price=?, last_check=? WHERE id=?", (seen, last, o["id"]))
                db.commit()
                if filled:
                    record_fill(db, mode, o, o["count"])
                    continue
        except ApiError as e:
            log.error("checking order on %s: %s", o["ticker"], e)
            continue
        m = markets.get(o["ticker"])
        want, _ = plan(m) if m else (None, "")
        why = ("market closed/expired" if not m or time.time() >= o["expire_at"] else
               "no longer a favorite in range" if not want or want["side"] != o["side"] else
               f"bid moved to {want['price']:.3f}" if abs(want["price"] - o["price"]) > REPEG else None)
        if why:
            if live:
                live_cancel(o["order_id"])
            close_order(db, o, "canceled", why)
            log.info("CANCEL %s %s @ %.3f: %s", o["ticker"], o["side"], o["price"], why)

    # 2. Risk gate (same rules as v1; resting orders count as money at work)
    opens = bot.open_trades(db, mode)
    resting = db.execute("SELECT * FROM maker_orders WHERE mode=? AND status='resting'", (mode,)).fetchall()
    at_work = sum(t["cost"] for t in opens) + sum(o["count"] * o["price"] for o in resting)
    balance = bot.get_balance() if live else bot.ACCOUNT_CAP + bot.pnl_total(db, mode) - at_work
    today = bot.pnl_today(db, mode) if live or PAPER_DAILY_LIMIT else 0.0   # the floor + HALT file still apply
    halt = bot.check_halt(balance + at_work, today)
    if halt:
        for o in resting:                                   # halted: leave nothing resting
            if live:
                live_cancel(o["order_id"])
            close_order(db, o, "canceled", "halt")
        if "floor" in halt:
            bot.HALT_FILE.write_text(f"{now_iso()} {halt}\n")
            bot.alert(f"HARD STOP: {halt}")
        else:
            log.warning("HALTED: %s", halt)
        return False

    # 3. Place new orders, at most MAX_PER_EVENT per event (strikes within an event are nearly the same bet)
    per_event = Counter([t["ticker"].rsplit("-", 1)[0] for t in opens] + [o["event"] for o in resting])
    taken = {t["ticker"] for t in opens} | {o["ticker"] for o in resting}
    slots = len(opens) + len(resting)
    for m in sorted(markets.values(), key=lambda m: m["close_time"]):
        if slots >= MAX_SLOTS:
            break
        if (per_event[m["event_ticker"]] >= MAX_PER_EVENT or m["ticker"] in taken
                or float(m.get("volume_fp") or 0) < 200):
            continue
        want, why = plan(m)
        if not want:
            continue
        # ponytail: size_order budgets for a taker fee we won't pay -> sizes a hair small. Fine.
        count = bot.size_order(want["price"], balance, at_work, 0)
        if not count:
            continue
        order_id = None
        if live:
            try:
                order_id = live_place(m["ticker"], want["side"], count, want["price"], want["expire_at"])
            except ApiError as e:
                if "HTTP 4" in str(e) and "HTTP 429" not in str(e):     # rejected (e.g. post_only would cross): no order exists
                    log.warning("REJECTED %s: %s", m["ticker"], e)
                    continue
                bot.HALT_FILE.write_text(f"{now_iso()} maker order error, check Kalshi for {m['ticker']}: {e}\n")
                bot.alert(f"maker order error on {m['ticker']}, halting: {e}")
                return False
        db.execute("INSERT INTO maker_orders(ts,mode,ticker,event,side,price,count,queue_ahead,placed_at,expire_at,"
                   "last_check,close_time,order_id,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (now_iso(), mode, m["ticker"], m["event_ticker"], want["side"], want["price"], count,
                    want["queue_ahead"], time.time(), want["expire_at"], time.time(), m["close_time"], order_id, why))
        db.commit()
        log.info("REST  [%s] %s bid %s x%d @ %.3f | %s", mode, m["ticker"], want["side"], count, want["price"], why)
        per_event[m["event_ticker"]] += 1; taken.add(m["ticker"]); slots += 1
        at_work += count * want["price"]; balance -= count * want["price"]
    log.info("cycle: %d markets, %d slots used (%d filled), $%.2f at work, today %+.2f, all-time %+.2f",
             len(markets), slots, len(opens), at_work, bot.pnl_today(db, mode), bot.pnl_total(db, mode))
    return True


def apply_profile(name, live):
    """Switch this process to a paper size profile; returns its journal mode. Never for live trading."""
    if live:
        sys.exit("PAPER_PROFILE is paper-only; unset it to trade live (live caps live in bot.py)")
    if name not in PROFILES:
        sys.exit(f"unknown PAPER_PROFILE {name!r}; known: {', '.join(PROFILES)}")
    prof = PROFILES[name]
    bot.ACCOUNT_CAP, bot.MAX_TRADE_COST, bot.DAILY_LOSS_LIMIT, bot.BALANCE_FLOOR = prof["caps"]
    globals().update({k: v for k, v in prof.items() if k != "caps"})
    bot.HALT_FILE = bot.HERE / f"HALT-{name}"
    return f"{MODE_PAPER}-{name}"


def report(db):
    for (mode,) in db.execute("SELECT DISTINCT mode FROM maker_orders ORDER BY mode").fetchall():
        n, filled, canceled = db.execute("SELECT count(*), sum(status='filled'), sum(status='canceled') FROM maker_orders "
                                         "WHERE mode=?", (mode,)).fetchone()
        if not n:
            continue
        k, wins, pnl, staked = db.execute("SELECT count(*), sum(pnl>0), sum(pnl), sum(cost) FROM trades WHERE mode=? "
                                          "AND status='settled'", (mode,)).fetchone()
        print(f"== {mode} ==\norders rested {n}: {filled or 0} filled ({(filled or 0) / n:.0%}), {canceled or 0} canceled unfilled")
        if k:
            print(f"settled {k}: {wins} won, {k - wins} lost, P&L ${pnl:+.2f} on ${staked:.2f} staked ({pnl / staked:+.1%}/$)")
            print(f"break-even needs a win rate of {staked / db.execute('SELECT sum(count) FROM trades WHERE mode=? AND status=?', (mode, 'settled')).fetchone()[0]:.1%}; actual {wins / k:.1%}")
            for r in db.execute("SELECT substr(ticker,1,instr(ticker,'-')-1) s, count(*), sum(pnl>0), round(sum(pnl),2) FROM trades "
                                "WHERE mode=? AND status='settled' GROUP BY s", (mode,)):
                print(f"  {r[0]:12s} {r[1]:4d} trades  {r[2]:4d} won  ${r[3]:+.2f}")
        print("Needs ~200+ settled trades before the P&L means anything: ~1 in 10-20 should lose its whole stake.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    bot.load_env()
    db = bot.open_db()
    init_db(db)
    if args.report:
        return report(db)
    live = os.environ.get("LIVE_TRADING") == "true"
    profile = os.environ.get("PAPER_PROFILE")
    mode = apply_profile(profile, live) if profile else MODE_LIVE if live else MODE_PAPER
    suffix = f"-{profile}" if profile else ""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(bot.HERE / f"maker{suffix}.log")])
    import fcntl                     # same lock file as bot.py: the two share one $10 cap, so never both at once
    lock = open(bot.HERE / f".lock{suffix}", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("this bot is already running in this folder; stop it first")
    log.info("starting %s%s (cap $%.0f, max $%.0f/trade, %s)", mode,
             " -- REAL MONEY" if mode == MODE_LIVE else " (no orders will be placed)",
             bot.ACCOUNT_CAP, bot.MAX_TRADE_COST,
             f"daily loss limit ${bot.DAILY_LOSS_LIMIT:.0f}" if live or PAPER_DAILY_LIMIT else "paper: no daily loss limit")
    while True:
        try:
            cycle(db, mode)
        except ApiError as e:
            log.error("cycle failed: %s", e)
        except Exception:
            log.exception("unexpected error; continuing")
        if args.once:
            break
        try:
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            break
    if mode == MODE_LIVE:            # never leave real orders resting once the bot has stopped
        for o in db.execute("SELECT * FROM maker_orders WHERE mode=? AND status='resting'", (mode,)).fetchall():
            live_cancel(o["order_id"])
            close_order(db, o, "canceled", "bot stopped")
        log.info("stopped; resting orders canceled (each also had a server-side expiry)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
