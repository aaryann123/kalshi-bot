#!/usr/bin/env python3
"""Kalshi 15-minute crypto market bot. Dry-run by default; LIVE_TRADING=true places real orders.

Usage:
  python bot.py --balance   # auth check: print account balance and exit
  python bot.py --once      # one evaluation cycle, then exit
  python bot.py             # loop forever (Ctrl-C to stop)
"""
import argparse, base64, logging, math, os, sqlite3, statistics, sys, time, uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).parent
KALSHI = "https://api.elections.kalshi.com"
API = "/trade-api/v2"

# ---- Risk parameters (edit here; see README) --------------------------------
ACCOUNT_CAP = 10.00       # hard ceiling on money the bot may have at work, whatever the API balance says
MAX_TRADE_COST = 2.00     # max $ per trade, fees included
MAX_OPEN_POSITIONS = 2
DAILY_LOSS_LIMIT = 3.00   # realized loss today (local date) that halts trading until tomorrow
BALANCE_FLOOR = 1.00      # equity below this => permanent halt until the HALT file is deleted

# ---- Strategy parameters ----------------------------------------------------
MIN_EDGE = 0.08           # required edge per contract AFTER fees, in dollars
MAX_SPREAD = 0.03         # skip if yes_ask - yes_bid is wider than this
MIN_MINUTES_LEFT = 2.0    # too close to expiry: quotes move faster than we poll
MAX_MINUTES_LEFT = 13.0   # too early: strike ~= spot, fair ~= 50%, nothing to say
MOMENTUM_WEIGHT = 0.5     # fraction of the last-5-min return assumed to continue. 0 disables momentum.
MAX_PRICE = 0.90          # never pay more than this: risk $0.90 to win $0.10 is a bad trade at our accuracy
TAKER_FEE_RATE = 0.07     # Kalshi taker fee = ceil(rate * count * P * (1-P)) to the cent
POLL_SECONDS = 30

# Rotation: only markets with a clean continuous underlying. Other 15-min series
# (KXCRYPTOLEAD15M coin race, KXCRYPTOCOMP15M, thin alts) are SKIPPED by design
# rather than traded on an order-book-imbalance fallback: on a $10 account an
# unexplainable signal isn't worth the fees.
SERIES = {
    "KXBTC15M": "BTC-USD",
    "KXETH15M": "ETH-USD",
    "KXSOL15M": "SOL-USD",
    "KXXRP15M": "XRP-USD",
    "KXDOGE15M": "DOGE-USD",
}

HALT_FILE = HERE / "HALT"
JOURNAL = Path(os.environ.get("KALSHI_JOURNAL") or HERE / "journal.db")  # point elsewhere to try reports on demo data
log = logging.getLogger("bot")


class ApiError(Exception):
    pass


def load_env():
    # ponytail: 6-line .env parser instead of a python-dotenv dependency
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


# ---- HTTP -------------------------------------------------------------------
def auth_headers(method, path):
    """RSA-PSS/SHA256 over `timestamp_ms + METHOD + path` (path includes /trade-api/v2, no query string)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key_id, key_path = os.environ.get("KALSHI_API_KEY_ID"), os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path:
        raise ApiError("KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH not set (see .env.example)")
    key = serialization.load_pem_private_key(Path(key_path).expanduser().read_bytes(), password=None)
    ts = str(int(time.time() * 1000))
    sig = key.sign(
        (ts + method + path).encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


def http(method, url, *, params=None, json=None, headers=None, retries=3):
    """One place for timeouts, rate limits and 5xx. Raises ApiError; never anything else."""
    for attempt in range(retries):
        try:
            r = requests.request(method, url, params=params, json=json, headers=headers, timeout=10)
            if r.status_code == 429 or r.status_code >= 500:
                raise ApiError(f"HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code >= 400:
                raise ApiError(f"HTTP {r.status_code} (not retried): {r.text[:300]}") from None
            return r.json()
        except (requests.RequestException, ValueError, ApiError) as e:
            if "not retried" in str(e) or attempt == retries - 1:
                raise ApiError(f"{method} {url}: {e}") from None
            log.warning("retry %d for %s %s: %s", attempt + 1, method, url, e)
            time.sleep(2 ** attempt)


def kalshi(method, path, *, auth=False, retries=3, **kw):
    full = API + path
    return http(method, KALSHI + full, headers=auth_headers(method, full) if auth else None, retries=retries, **kw)


def get_balance():
    return kalshi("GET", "/portfolio/balance", auth=True)["balance"] / 100  # API reports cents


# ---- Signal -----------------------------------------------------------------
def fee(count, price):
    return math.ceil(TAKER_FEE_RATE * count * price * (1 - price) * 100 - 1e-9) / 100


def fair_yes(spot, strike, mins_left, sigma_per_min, ret_5m):
    """P(price at expiry >= strike) under a driftless-plus-momentum lognormal walk.

    ponytail: ignores that settlement is a 60s average of CF Benchmarks RTI (not
    Coinbase spot). That basis is a few bps; MIN_EDGE is the cushion for it. If
    live results disagree with dry-run fair values, this is the first suspect.
    """
    drift = MOMENTUM_WEIGHT * ret_5m * min(mins_left, 5) / 5
    z = (math.log(spot / strike) + drift) / (sigma_per_min * math.sqrt(mins_left))
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def underlying(product):
    """(spot, last-5-min log return, per-minute vol) from Coinbase.
    Spot comes from the live ticker: the candles endpoint lags 2-4 minutes (measured),
    which is fine for volatility and the 5-min-ago reference but useless as "now"."""
    base = f"https://api.exchange.coinbase.com/products/{product}"
    tick = http("GET", base + "/ticker")
    rows = http("GET", base + "/candles", params={"granularity": 60})  # [start_ts, low, high, open, close, vol], newest first
    try:
        spot = (float(tick["bid"]) + float(tick["ask"])) / 2  # mid, not last trade: no bid/ask bounce
        age = time.time() - datetime.fromisoformat(tick["time"].replace("Z", "+00:00")).timestamp()
        closes = [r[4] for r in rows[:61]]
        # close of the candle covering (now - 5min); candle i closes at start_ts + 60
        ref = next(r[4] for r in rows if r[0] + 60 <= time.time() - 300)
    except (KeyError, TypeError, ValueError, StopIteration, IndexError) as e:
        raise ApiError(f"coinbase {product}: bad data ({e!r})") from None
    if age > 30 or len(closes) < 30 or time.time() - rows[0][0] > 600:
        raise ApiError(f"coinbase {product}: stale (last trade {age:.0f}s ago)")
    rets = [math.log(a / b) for a, b in zip(closes, closes[1:])]
    return spot, math.log(spot / ref), max(statistics.pstdev(rets), 1e-6)


def evaluate(m, spot, ret_5m, sigma, now=None):
    """Pure decision for one market. Returns dict with action 'buy'/'skip', side, price, edge, reason."""
    now = now or time.time()
    close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()
    mins = (close - now) / 60
    out = {"ticker": m["ticker"], "spot": spot, "strike": m.get("floor_strike"), "mins_left": round(mins, 2),
           "action": "skip", "side": None, "price": None, "edge": None, "fair": None}
    try:
        yb, ya, na = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"]), float(m["no_ask_dollars"])
    except (KeyError, ValueError):
        return {**out, "reason": "no quotes"}
    out.update(yes_bid=yb, yes_ask=ya)
    if not out["strike"] or m.get("strike_type") != "greater_or_equal":
        return {**out, "reason": "no strike yet / unknown strike type"}
    if not MIN_MINUTES_LEFT <= mins <= MAX_MINUTES_LEFT:
        return {**out, "reason": f"{mins:.1f} min left outside [{MIN_MINUTES_LEFT}, {MAX_MINUTES_LEFT}]"}
    if ya <= 0 or na <= 0 or yb <= 0:
        return {**out, "reason": "one-sided book"}
    if ya - yb > MAX_SPREAD + 1e-9:  # float noise: 0.52 - 0.49 == 0.030000000000000027
        return {**out, "reason": f"spread {ya - yb:.2f} > {MAX_SPREAD}"}

    fair = fair_yes(spot, out["strike"], mins, sigma, ret_5m)
    edges = {"yes": fair - ya - fee(1, ya), "no": (1 - fair) - na - fee(1, na)}
    side = max(edges, key=edges.get)
    price = ya if side == "yes" else na
    out.update(fair=round(fair, 4), side=side, price=price, edge=round(edges[side], 4))
    why = (f"spot {spot:g} vs strike {out['strike']:g} ({(spot / out['strike'] - 1) * 1e4:+.1f}bp), "
           f"5m ret {ret_5m * 1e4:+.1f}bp, vol {sigma * 1e4:.1f}bp/min, {mins:.1f}m left -> fair YES {fair:.2f}; "
           f"market YES {yb:.2f}/{ya:.2f}; best side {side} @ {price:.2f}, edge after fee {edges[side]:+.3f}")
    if price > MAX_PRICE:
        return {**out, "reason": f"price {price:.2f} > MAX_PRICE. {why}"}
    if edges[side] < MIN_EDGE:
        return {**out, "reason": f"edge below {MIN_EDGE}. {why}"}
    return {**out, "action": "buy", "reason": why}


# ---- Risk -------------------------------------------------------------------
def check_halt(equity, pnl_today):
    """Returns a halt reason or None. equity = available balance + cost of open positions."""
    if HALT_FILE.exists():
        return f"HALT file present ({HALT_FILE.read_text().strip()}); delete it to resume"
    if equity < BALANCE_FLOOR:
        return f"equity ${equity:.2f} below ${BALANCE_FLOOR:.2f} floor"
    if pnl_today <= -DAILY_LOSS_LIMIT:
        return f"daily loss ${-pnl_today:.2f} hit ${DAILY_LOSS_LIMIT:.2f} limit"
    return None


def size_order(price, api_balance, open_cost, open_count):
    """Whole contracts to buy, 0 if not allowed. The cap is ours, not the API's:
    a $5,000 reported balance still yields at most ACCOUNT_CAP total at work."""
    if open_count >= MAX_OPEN_POSITIONS or not 0 < price < 1:
        return 0
    budget = min(MAX_TRADE_COST, ACCOUNT_CAP - open_cost, api_balance)
    n = max(0, int(budget / price))
    while n and n * price + fee(n, price) > budget + 1e-9:
        n -= 1
    return n


# ---- Journal ----------------------------------------------------------------
def open_db(path=None):
    db = sqlite3.connect(path or JOURNAL)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS signals(ts TEXT, mode TEXT, ticker TEXT, spot REAL, strike REAL, mins_left REAL,
        yes_bid REAL, yes_ask REAL, fair REAL, side TEXT, price REAL, edge REAL, action TEXT, reason TEXT);
    CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, ts TEXT, mode TEXT, ticker TEXT, side TEXT,
        count INTEGER, price REAL, fee REAL, cost REAL, order_id TEXT, reason TEXT,
        status TEXT DEFAULT 'open', result TEXT, pnl REAL, settled_ts TEXT);
    """)
    return db


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_signal(db, mode, s):
    db.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (now_iso(), mode, s["ticker"], s["spot"], s["strike"], s["mins_left"], s.get("yes_bid"),
                s.get("yes_ask"), s["fair"], s["side"], s["price"], s["edge"], s["action"], s["reason"]))
    db.commit()


def open_trades(db, mode):
    return db.execute("SELECT * FROM trades WHERE mode=? AND status='open'", (mode,)).fetchall()


def pnl_today(db, mode):
    # settled_ts is UTC; compare on local date so "the day" matches the user's day
    return db.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE mode=? AND status='settled' "
                      "AND date(settled_ts,'localtime')=date('now','localtime')", (mode,)).fetchone()[0]


def pnl_total(db, mode):
    return db.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE mode=? AND status='settled'", (mode,)).fetchone()[0]


def settle(db, mode):
    for t in open_trades(db, mode):
        try:
            result = kalshi("GET", f"/markets/{t['ticker']}")["market"].get("result")
        except ApiError as e:
            log.error("settle check failed for %s: %s", t["ticker"], e)
            continue
        if result in ("yes", "no"):
            pnl = round((t["count"] if result == t["side"] else 0) - t["cost"], 2)
            db.execute("UPDATE trades SET status='settled', result=?, pnl=?, settled_ts=? WHERE id=?",
                       (result, pnl, now_iso(), t["id"]))
            db.commit()
            log.info("SETTLED [%s] %s %s x%d -> %s, pnl %+.2f | today %+.2f, all-time %+.2f", mode, t["ticker"],
                     t["side"], t["count"], result, pnl, pnl_today(db, mode), pnl_total(db, mode))


# ---- Execution --------------------------------------------------------------
def place_live_order(ticker, side, count, price):
    """IOC limit at the quoted ask: fills now or not at all, so there are never resting orders to babysit.
    V2 endpoint quotes from the YES side: buy NO at p == sell YES (ask) at 1-p.
    Returns (filled_count, cost_per_contract, fee_total, order_id)."""
    # Last line of defence, independent of the caller (not `assert`: python -O strips those).
    if os.environ.get("LIVE_TRADING") != "true":
        raise RuntimeError("live order attempted without LIVE_TRADING=true")
    if count * price + fee(count, price) > min(MAX_TRADE_COST, ACCOUNT_CAP) + 1e-9:
        raise RuntimeError("order exceeds risk limits")
    body = {"ticker": ticker, "client_order_id": str(uuid.uuid4()),
            "side": "bid" if side == "yes" else "ask",
            "count": f"{count}.00", "price": f"{price if side == 'yes' else 1 - price:.4f}",
            "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}
    # retries=1: never auto-retry an order POST; a timeout could mean it went through
    r = kalshi("POST", "/portfolio/events/orders", auth=True, retries=1, json=body)
    filled = int(float(r["fill_count"]))
    if not filled:
        return 0, 0.0, 0.0, r["order_id"]
    avg = float(r["average_fill_price"])
    return filled, (avg if side == "yes" else 1 - avg), float(r.get("average_fee_paid", 0)) * filled, r["order_id"]


def alert(text):
    log.critical(text)
    url = os.environ.get("NOTIFY_URL")  # optional webhook, e.g. a local chat bridge; POSTed {"text": ...}
    if not url:
        return
    try:
        requests.post(url, json={"text": f"kalshi-bot: {text}"}, timeout=3)
    except requests.RequestException:
        pass


def cycle(db, mode):
    settle(db, mode)
    opens = open_trades(db, mode)
    open_cost = sum(t["cost"] for t in opens)
    # Dry-run balance is simulated from the journal; live balance comes from the API.
    balance = get_balance() if mode == "live" else ACCOUNT_CAP + pnl_total(db, mode) - open_cost
    halt = check_halt(balance + open_cost, pnl_today(db, mode))
    if halt:
        if "floor" in halt:
            HALT_FILE.write_text(f"{now_iso()} {halt}\n")
            alert(f"HARD STOP: {halt}")
        else:
            log.warning("HALTED: %s", halt)
        return False

    held = {t["ticker"] for t in opens}
    for series, product in SERIES.items():
        try:
            markets = kalshi("GET", "/markets", params={"series_ticker": series, "status": "open"})["markets"]
            if not markets:
                continue
            spot, ret_5m, sigma = underlying(product)
        except ApiError as e:
            log.error("%s: %s", series, e)
            continue
        for m in markets:
            s = evaluate(m, spot, ret_5m, sigma)
            if s["action"] == "buy" and s["ticker"] in held:
                s.update(action="skip", reason="already holding. " + s["reason"])
            count = size_order(s["price"], balance, open_cost, len(held)) if s["action"] == "buy" else 0
            if s["action"] == "buy" and not count:
                s.update(action="skip", reason="risk limits allow 0 contracts. " + s["reason"])
            log_signal(db, mode, s)
            log.info("%-4s %s | %s", s["action"].upper(), s["ticker"], s["reason"])
            if s["action"] != "buy":
                continue

            price, f, order_id = s["price"], fee(count, s["price"]), None
            if mode == "live":
                try:
                    count, price, f, order_id = place_live_order(s["ticker"], s["side"], count, price)
                except ApiError as e:
                    # Unknown outcome (e.g. timeout): stop rather than risk stacking orders blind.
                    HALT_FILE.write_text(f"{now_iso()} order error, check Kalshi for {s['ticker']}: {e}\n")
                    alert(f"order error on {s['ticker']}, halting: {e}")
                    return False
                if not count:
                    log.info("NOFILL %s (IOC found no liquidity at %.2f)", s["ticker"], s["price"])
                    continue
            cost = round(count * price + f, 2)
            db.execute("INSERT INTO trades(ts,mode,ticker,side,count,price,fee,cost,order_id,reason) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (now_iso(), mode, s["ticker"], s["side"], count, price, f, cost, order_id, s["reason"]))
            db.commit()
            log.info("TRADE [%s] %s buy %s x%d @ %.2f, fee %.2f, cost %.2f", mode, s["ticker"], s["side"], count, price, f, cost)
            held.add(s["ticker"]); open_cost += cost; balance -= cost
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--balance", action="store_true", help="print account balance and exit (auth check)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = ap.parse_args()
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(HERE / "bot.log")])
    if args.balance:
        print(f"Kalshi balance: ${get_balance():.2f}")
        return
    # Single instance only: two bots on one journal race past MAX_OPEN_POSITIONS (and in live mode,
    # double the risk). flock dies with the process, so there's never a stale lock to clean up.
    import fcntl
    lock = open(HERE / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another bot.py is already running in this folder; stop it first")
    mode = "live" if os.environ.get("LIVE_TRADING") == "true" else "dry"
    log.info("starting in %s mode%s", mode.upper(), " -- REAL MONEY" if mode == "live" else " (no orders will be placed)")
    db = open_db()
    while True:
        try:
            cycle(db, mode)
        except ApiError as e:
            log.error("cycle failed: %s", e)
        except Exception:
            log.exception("unexpected error; continuing")
        if args.once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
