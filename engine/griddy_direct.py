"""griddy_direct.py — GRIDDY DIRECT: grid execution straight on Coinbase.

WHY (Ash, 2026-09-04): 3Commas' only remaining job under Zero was "hold a ladder,
replace filled rungs" — and it cost a subscription, a 3-week write-block hostage
situation, and an entire class of losses from its enable-time balancing trades.
Direct execution makes market-trading base STRUCTURALLY impossible: the ladder is
only ever resting post-only limit orders.

PAPER MODE (current): simulates the ladder against live spot — no orders placed,
no auth needed. Validates fill logic, pairing, and P&L for days before any live
order. LIVE mode (later, only on Ash's explicit go): same state machine with
place/cancel wired to the Advanced Trade API (COINBASE_TRADE_* key), post-only,
idempotent client_order_ids, reconciliation loop, cancel-all kill switch.

State: direct_state.json (atomic writes — the 2026-08-11 corruption lesson).
Rungs: {id, side, price, size, status open|filled, paired_from}. On a BUY fill →
place SELL one step up; SELL fill → BUY one step down. Classic grid, nothing else.
"""
import os
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "direct_state.json")
FILLS_FILE = os.path.join(HERE, "direct_paper_fills.jsonl")

FEE_RATE = 0.0007      # observed via order preview 2026-09-04 (0.07%/side)


def _load():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"active": False}


def _save(s):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def get_state():
    return _load()


def build_ladder(price, portfolio_usd=70000.0):
    """Nested static ladder centred at price (same geometry as zero_mode), sized
    by the classic tier budgets. Rungs below price = BUY, above = SELL (paper
    assumes we hold the BTC to back sells; live mode will size sells to actual
    holdings)."""
    import zero_mode
    tiers = zero_mode.make_static_tiers(price)
    budgets = {"inner": 0.38, "mid": 0.31, "outer": 0.26}
    rungs = []
    n = 0
    for t in tiers:
        cap = portfolio_usd * 0.9 * budgets.get(t["name"], 0.3)
        size = round(cap / t["levels"] / price, 6)
        for lvl in t["grid_levels"]:
            if abs(lvl - price) < t["step"] * 0.25:
                continue   # skip the rung sitting on top of price
            n += 1
            rungs.append({"id": "r%d" % n, "tier": t["name"], "step": t["step"],
                          "side": "BUY" if lvl < price else "SELL",
                          "price": round(lvl, 2), "size": size, "status": "open"})
    return {"active": True, "mode": "paper", "created": time.time(),
            "anchor_price": price, "portfolio_usd": portfolio_usd,
            "rungs": rungs, "cash": 0.0, "btc": 0.0,
            "realised": 0.0, "fees": 0.0, "fills": 0, "last_price": price,
            "last_tick": time.time()}


def activate_paper(price, portfolio_usd=70000.0):
    s = build_ladder(price, portfolio_usd)
    _save(s)
    return s


def tick(price):
    """Advance the paper simulation to `price`. Fills any rung the price crossed
    since last tick, places the paired rung, books cash/btc/fees. Conservative:
    spot polling misses wicks, so paper UNDERcounts fills vs reality."""
    s = _load()
    if not s.get("active") or s.get("mode") != "paper":
        return None
    last = s.get("last_price") or price
    lo, hi = min(last, price), max(last, price)
    filled = []
    max_id = max((int(r["id"][1:]) for r in s["rungs"]), default=0)
    for r in s["rungs"]:
        if r["status"] != "open":
            continue
        # BUY fills if price crossed DOWN through the rung; SELL if crossed UP
        hit = (r["side"] == "BUY" and price <= r["price"] <= last) or \
              (r["side"] == "SELL" and last <= r["price"] <= price)
        if not hit:
            continue
        r["status"] = "filled"
        fee = r["size"] * r["price"] * FEE_RATE
        s["fees"] += fee
        s["fills"] += 1
        if r["side"] == "BUY":
            s["btc"] += r["size"]
            s["cash"] -= r["size"] * r["price"] + fee
            new = {"side": "SELL", "price": round(r["price"] + r["step"], 2)}
        else:
            s["btc"] -= r["size"]
            s["cash"] += r["size"] * r["price"] - fee
            s["realised"] += r["size"] * r["step"] - fee * 2   # rung spread net of RT fee
            new = {"side": "BUY", "price": round(r["price"] - r["step"], 2)}
        max_id += 1
        s["rungs"].append({"id": "r%d" % max_id, "tier": r["tier"], "step": r["step"],
                           "side": new["side"], "price": new["price"],
                           "size": r["size"], "status": "open",
                           "paired_from": r["id"]})
        filled.append({"ts": int(time.time()), "side": r["side"],
                       "price": r["price"], "size": r["size"], "tier": r["tier"]})
    s["last_price"] = price
    s["last_tick"] = time.time()
    _save(s)
    if filled:
        try:
            with open(FILLS_FILE, "a") as f:
                for x in filled:
                    f.write(json.dumps(x) + "\n")
        except Exception:
            pass
    return filled


def summary():
    s = _load()
    if not s.get("active"):
        return {"active": False}
    px = s.get("last_price") or 0
    open_b = sum(1 for r in s["rungs"] if r["status"] == "open" and r["side"] == "BUY")
    open_s = sum(1 for r in s["rungs"] if r["status"] == "open" and r["side"] == "SELL")
    mtm = s["cash"] + s["btc"] * px
    return {"active": True, "mode": s["mode"],
            "since": s.get("created"), "anchor_price": s.get("anchor_price"),
            "last_price": px, "age_min": round((time.time() - s.get("last_tick", 0)) / 60, 1),
            "open_rungs": {"buy": open_b, "sell": open_s},
            "fills": s["fills"], "paper_btc": round(s["btc"], 6),
            "paper_cash": round(s["cash"], 2), "fees_paid": round(s["fees"], 2),
            "rung_spread_realised": round(s["realised"], 2),
            "mark_to_market_pnl": round(mtm, 2)}
