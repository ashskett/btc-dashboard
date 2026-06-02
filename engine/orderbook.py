"""Coinbase L2 order-book liquidity collector — Phase 0 (observability only).

Reads the Coinbase BTC/USDC order book once per engine cycle (via the existing
ccxt instance in ``market_data``), aggregates the raw levels into price buckets,
detects significant bid/ask "walls", and — crucially — tracks how many
consecutive cycles each wall has survived. **Persistence is the real signal**:
instantaneous size is spoofable, a wall that has rested across many cycles is
not. The 2-minute engine cadence means we can only read slow, structural
features anyway, which is exactly what a grid cares about.

What this module is NOT: it makes **no trading decisions** and nothing it
produces is consumed by the engine's bot logic. Its entire job is to gather (and
persist) the data needed to later build — and backtest — order-book-aware
recentre / breakout logic (Phases 1-3). Side effects per cycle: one REST call,
one appended line to ``orderbook_log.jsonl``, and a small state file rewrite.

Execution-venue caveat: Coinbase BTC/USDC spot is dense near mid and thin far
out (≈±1×ATR of usable depth even 1000 levels deep). The large round-number
walls seen on aggregated multi-venue heatmaps are a cross-venue/perp phenomenon
and are deliberately out of scope here — that's the parked "aggregate feed"
(Phase 2). This collector measures the liquidity our orders actually hit.
"""
import json
import os
import time
from statistics import median

import market_data  # reuse the module-level ccxt.coinbase() instance

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "orderbook_log.jsonl")
STATE_FILE = os.path.join(HERE, "orderbook_state.json")

# ── Tunables ────────────────────────────────────────────────────────────────
SYMBOL = "BTC/USDC"     # the execution venue/pair — where 3Commas actually fills
DEPTH_LIMIT = 1000      # raw levels per side to request (Coinbase honours up to 1000)
BUCKET_USD = 50.0       # aggregate raw levels into $50 price buckets
WALL_MULT = 4.0         # a bucket is a "wall" if size >= WALL_MULT × median bucket size
TOP_N = 6               # keep this many strongest walls per side
NEAR_ATR = 2.0          # summary "nearest wall" search is limited to ±NEAR_ATR×ATR
MIN_BUCKETS = 4         # need at least this many non-empty buckets for a median to mean anything


# ── State (wall persistence across cycles) ───────────────────────────────────
def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"bid": {}, "ask": {}}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:  # noqa: BLE001
        print(f"Warning: could not write {os.path.basename(STATE_FILE)}: {e}")


# ── Pure helpers ─────────────────────────────────────────────────────────────
def _bucketize(levels) -> dict:
    """Aggregate [[price, size], ...] into {bucket_price: total_size}."""
    buckets: dict = {}
    for price, size in levels:
        b = round(price / BUCKET_USD) * BUCKET_USD
        buckets[b] = buckets.get(b, 0.0) + float(size)
    return buckets


def _find_walls(buckets: dict) -> list:
    """Return up to TOP_N buckets whose size stands out vs the median bucket."""
    if len(buckets) < MIN_BUCKETS:
        return []
    med = median(buckets.values())
    if med <= 0:
        return []
    thr = med * WALL_MULT
    walls = [{"price": float(p), "size": round(s, 4)}
             for p, s in buckets.items() if s >= thr]
    walls.sort(key=lambda w: w["size"], reverse=True)
    return walls[:TOP_N]


def _apply_persistence(side_key: str, walls: list, state: dict) -> list:
    """Carry a per-bucket survival counter across cycles. A wall present last
    cycle (same $50 bucket) increments; an absent bucket resets to 0/gone."""
    prev = state.get(side_key, {})
    cur: dict = {}
    for w in walls:
        key = str(int(w["price"]))
        w["persistence"] = int(prev.get(key, 0)) + 1
        cur[key] = w["persistence"]
    state[side_key] = cur
    return walls


def _annotate(w: dict, mid: float, atr: float) -> dict:
    d = w["price"] - mid
    return {
        "price": round(w["price"], 1),
        "size": w["size"],
        "persistence": w["persistence"],
        "dist_usd": round(d, 1),
        "dist_atr": round(d / atr, 2) if atr else None,
    }


def _nearest(walls: list, mid: float, atr: float, below: bool):
    """Closest wall on one side within ±NEAR_ATR×ATR of mid, annotated."""
    lim = NEAR_ATR * atr
    if below:
        cands = [w for w in walls if 0 <= (mid - w["price"]) <= lim]
        if not cands:
            return None
        return _annotate(max(cands, key=lambda w: w["price"]), mid, atr)
    cands = [w for w in walls if 0 <= (w["price"] - mid) <= lim]
    if not cands:
        return None
    return _annotate(min(cands, key=lambda w: w["price"]), mid, atr)


# ── Public entry point ───────────────────────────────────────────────────────
def snapshot(price: float, atr: float, write_log: bool = True) -> dict:
    """Fetch the book, detect persistent walls, return a compact summary and
    (by default) append a full record to orderbook_log.jsonl.

    Returns a small dict safe to embed in engine_status.json. Raises on a failed
    fetch — the caller MUST wrap this so a book outage never breaks the cycle.
    """
    atr = atr or (price * 0.01)

    ob = market_data.exchange.fetch_order_book(SYMBOL, limit=DEPTH_LIMIT)
    bids = ob.get("bids") or []   # descending by price: [[price, size], ...]
    asks = ob.get("asks") or []   # ascending by price
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid = (best_bid + best_ask) / 2.0 if (best_bid and best_ask) else price
    spread = (best_ask - best_bid) if (best_bid and best_ask) else None

    bid_buckets = _bucketize(bids)
    ask_buckets = _bucketize(asks)
    bid_depth = round(sum(bid_buckets.values()), 3)
    ask_depth = round(sum(ask_buckets.values()), 3)
    total = bid_depth + ask_depth
    # +1.0 = book is all bids (support-heavy), -1.0 = all asks (resistance-heavy)
    imbalance = round((bid_depth - ask_depth) / total, 4) if total else 0.0

    state = _load_state()
    bid_walls = _apply_persistence("bid", _find_walls(bid_buckets), state)
    ask_walls = _apply_persistence("ask", _find_walls(ask_buckets), state)
    _save_state(state)

    summary = {
        "mid": round(mid, 1),
        "spread": round(spread, 2) if spread is not None else None,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "imbalance": imbalance,
        "nearest_bid_wall": _nearest(bid_walls, mid, atr, below=True),
        "nearest_ask_wall": _nearest(ask_walls, mid, atr, below=False),
        "n_bid_walls": len(bid_walls),
        "n_ask_walls": len(ask_walls),
        "depth_span_usd": round((bids[0][0] - bids[-1][0]), 0) if len(bids) > 1 else 0,
    }

    if write_log:
        record = dict(summary)
        record.update({
            "ts": round(time.time(), 1),
            "price": round(price, 1),
            "atr": round(atr, 1),
            "bid_walls": [_annotate(w, mid, atr) for w in bid_walls],
            "ask_walls": [_annotate(w, mid, atr) for w in ask_walls],
        })
        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:  # noqa: BLE001
            print(f"Warning: could not write {os.path.basename(LOG_FILE)}: {e}")

    return summary


if __name__ == "__main__":
    # Manual smoke test: python orderbook.py  (does NOT write the log by default)
    import sys
    write = "--log" in sys.argv
    # fetch a live price to give ATR/mid something real
    df = market_data.get_btc_data()
    px = float(df["close"].iloc[-1])
    hi, lo, cl = df["high"], df["low"], df["close"]
    tr = (hi - lo).rolling(14).mean().iloc[-1]
    s = snapshot(px, float(tr), write_log=write)
    print(json.dumps(s, indent=2))
