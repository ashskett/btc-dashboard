"""Multi-venue order-book liquidity aggregator.

Coinbase's spot book is thin (~±1xATR), so the engine's own orderbook.py only
ever sees walls right next to price. This aggregates the DEEP books across
several venues (Binance/Bybit/OKX/Kraken) into one combined liquidity map and
surfaces durable walls — including ones FAR from price that Coinbase can't show.

OBSERVABILITY ONLY (Phase: data collection). It does NOT trade and is NOT wired
into any engine decision. It runs as its own process (cron), independent of the
engine cycle, so it never adds latency to trading. Writes:
  - multi_orderbook_state.json  (latest snapshot, for the dashboard)
  - multi_orderbook_log.jsonl   (history, for backtesting the far-wall hypothesis)

Persistence (how many consecutive runs a wall has held) is tracked across runs
via the state file, so we can later test whether durable far walls actually hold.
"""
import os
import json
import time
import statistics

import ccxt

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "multi_orderbook_state.json")
LOG_FILE = os.path.join(HERE, "multi_orderbook_log.jsonl")
ENGINE_STATUS = os.path.join(HERE, "engine_status.json")

# Deep BTC books. USDT≈USDC≈USD for liquidity purposes. (limit = depth of levels)
VENUES = [
    ("binance", "BTC/USDT", 5000),
    ("okx",     "BTC/USDT", 400),
    ("bybit",   "BTC/USDT", 200),
    ("kraken",  "BTC/USD",  500),
]
BUCKET = 50.0          # $ price bucket
WALL_MULT = 4.0        # a bucket is a "wall" if >= this x median bucket size
WALL_MIN_BTC = 10.0    # ...and at least this many BTC
MAX_DIST_PCT = 0.06    # ignore walls beyond ±6% of price
TOP_N = 10             # keep this many walls per side


def _atr() -> float:
    try:
        with open(ENGINE_STATUS) as f:
            return float(json.load(f).get("atr") or 0) or 400.0
    except Exception:
        return 400.0


def _fetch(name, sym, lim):
    try:
        ex = getattr(ccxt, name)({"enableRateLimit": True, "timeout": 15000})
        ob = ex.fetch_order_book(sym, limit=lim)
        return ob.get("bids") or [], ob.get("asks") or []
    except Exception:
        return None, None


def aggregate():
    bids, asks, mids, used = {}, {}, [], []
    for name, sym, lim in VENUES:
        b, a = _fetch(name, sym, lim)
        if not b or not a:
            continue
        used.append(name)
        mids.append((float(b[0][0]) + float(a[0][0])) / 2)
        for lv in b:
            k = round(float(lv[0]) / BUCKET) * BUCKET
            bids[k] = bids.get(k, 0.0) + float(lv[1])
        for lv in a:
            k = round(float(lv[0]) / BUCKET) * BUCKET
            asks[k] = asks.get(k, 0.0) + float(lv[1])
    mid = statistics.median(mids) if mids else 0.0
    return bids, asks, mid, used


def _walls(agg, mid, atr):
    if not agg or not mid:
        return []
    sizes = [v for v in agg.values() if v > 0]
    thr = max(statistics.median(sizes) * WALL_MULT, WALL_MIN_BTC)
    out = []
    for price, size in agg.items():
        if size < thr:
            continue
        if abs(price - mid) / mid > MAX_DIST_PCT:
            continue
        out.append({"price": price, "size": round(size, 2),
                    "dist_usd": round(price - mid, 1),
                    "dist_atr": round((price - mid) / atr, 2) if atr else None})
    out.sort(key=lambda w: -w["size"])
    return out[:TOP_N]


def _apply_persistence(walls, prev):
    """Carry a 'persistence' count for walls that held at ~the same price (±1
    bucket) on the previous run."""
    prevmap = {round(w["price"] / BUCKET) * BUCKET: w.get("persistence", 1) for w in prev}
    for w in walls:
        k = round(w["price"] / BUCKET) * BUCKET
        w["persistence"] = prevmap.get(k, 0) + 1
    return walls


def collect():
    atr = _atr()
    bids, asks, mid, used = aggregate()
    if not used or not mid:
        return {"ok": False, "reason": "no venues"}
    prev = {}
    if os.path.exists(STATE_FILE):
        try:
            prev = json.load(open(STATE_FILE))
        except Exception:
            prev = {}
    bid_walls = _apply_persistence(_walls(bids, mid, atr), prev.get("bid_walls", []))
    ask_walls = _apply_persistence(_walls(asks, mid, atr), prev.get("ask_walls", []))
    state = {
        "ts": round(time.time()),
        "mid": round(mid, 1),
        "atr": round(atr, 1),
        "venues": used,
        "bid_walls": bid_walls,
        "ask_walls": ask_walls,
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    # append a compact line to the history log
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(state) + "\n")
    return {"ok": True, "venues": used, "mid": mid,
            "n_bid": len(bid_walls), "n_ask": len(ask_walls)}


if __name__ == "__main__":
    res = collect()
    print("multi_orderbook:", res)
    if res.get("ok"):
        s = json.load(open(STATE_FILE))
        print("  mid $%.0f via %s" % (s["mid"], ",".join(s["venues"])))
        for side in ("bid_walls", "ask_walls"):
            print("  %s:" % side)
            for w in s[side][:6]:
                print("    $%-8.0f %7.1f BTC | %+6.0f (%+.2fATR) persist=%d" % (
                    w["price"], w["size"], w["dist_usd"], w["dist_atr"] or 0, w["persistence"]))
