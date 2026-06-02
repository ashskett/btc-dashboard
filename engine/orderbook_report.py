#!/usr/bin/env python3
"""Order-book collector report — Phase 0 analysis hook (read-only).

Two jobs, both read-only:

1. **Collector health** — confirm orderbook.py is actually gathering usable data:
   cycle count, time span, cadence, how often a *persistent* wall is present,
   the persistence distribution, and book imbalance / depth span.

2. **Recentre-coincidence seed (the Phase-1 question)** — for every grid recentre
   in the window, join the nearest order-book snapshot and ask: *did a persistent
   wall sit between the old grid centre and the price we recentred toward?* That
   is exactly the future "order-book veto" condition. Cross-tabbed against whether
   the recentre was a dud (<2 fills in the next ~hour, reusing backtest.py), it
   answers "would the book have predicted the duds?" — but only once enough
   recentres overlap the collected book history (needs ~1-2 weeks of data).

Nothing here changes trading logic. Usage on the droplet:
    venv/bin/python orderbook_report.py
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OB_LOG = os.path.join(HERE, "orderbook_log.jsonl")
ENGINE_LOG = os.path.join(HERE, "engine_log.jsonl")
FILLS_LOG = os.path.join(HERE, "fills_log.jsonl")

PERSIST_MIN = 3      # cycles a wall must survive to count as "durable"
JOIN_TOL_S = 120     # max seconds between a recentre and the nearest book snapshot
FWD_CYCLES = 30      # ~60 min payoff window after a recentre
DUD_FILLS = 2        # <2 fills in the window = a dud recentre
MIN_RECENTRES = 8    # below this, the coincidence stats aren't worth reporting


def _load_ob():
    if not os.path.exists(OB_LOG):
        return []
    out = []
    with open(OB_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    out.sort(key=lambda r: r.get("ts", 0))
    return out


def _pct(n, d):
    return f"{(100.0 * n / d):.0f}%" if d else "n/a"


def collector_health(ob):
    print("── Collector health ──────────────────────────────────────────")
    if not ob:
        print("  No order-book data yet. The collector was just deployed; this")
        print("  fills in from the next engine cycle. Re-run in a few hours.")
        return False
    n = len(ob)
    span_h = (ob[-1]["ts"] - ob[0]["ts"]) / 3600.0
    first = datetime.fromtimestamp(ob[0]["ts"], timezone.utc)
    last = datetime.fromtimestamp(ob[-1]["ts"], timezone.utc)
    has_bid = sum(1 for r in ob if r.get("nearest_bid_wall"))
    has_ask = sum(1 for r in ob if r.get("nearest_ask_wall"))

    def _durable(r, side):
        return any(w.get("persistence", 0) >= PERSIST_MIN for w in r.get(side, []))
    dur_bid = sum(1 for r in ob if _durable(r, "bid_walls"))
    dur_ask = sum(1 for r in ob if _durable(r, "ask_walls"))
    max_persist = max((w.get("persistence", 0)
                       for r in ob for w in r.get("bid_walls", []) + r.get("ask_walls", [])),
                      default=0)
    imbs = [r.get("imbalance", 0.0) for r in ob]
    spans = [r.get("depth_span_usd", 0) for r in ob if r.get("depth_span_usd")]

    print(f"  cycles: {n}  |  span: {span_h:.1f}h ({span_h/24:.1f}d)")
    print(f"  window: {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M} UTC")
    print(f"  cadence: {span_h * 60 / max(n - 1, 1):.1f} min/cycle (expect ~2)")
    print(f"  nearest bid wall present: {_pct(has_bid, n)}   ask: {_pct(has_ask, n)}")
    print(f"  DURABLE (≥{PERSIST_MIN}c) wall present — bid: {_pct(dur_bid, n)}  ask: {_pct(dur_ask, n)}")
    print(f"  max persistence seen: {max_persist} cycles ({max_persist * 2} min)")
    print(f"  mean imbalance: {sum(imbs)/len(imbs):+.3f}  (+bid-heavy / -ask-heavy)")
    if spans:
        print(f"  mean bid-side depth span: ${sum(spans)/len(spans):,.0f}")
    return True


def _nearest_ob(ts, ob):
    best, bd = None, JOIN_TOL_S + 1
    for r in ob:
        d = abs(r.get("ts", 0) - ts)
        if d < bd:
            best, bd = r, d
    return best if bd <= JOIN_TOL_S else None


def _blocking_wall(rec, old_center, price):
    """Was there a durable wall between the old grid centre and the price we
    recentred toward? That's the future veto trigger (price likely to revert)."""
    if old_center is None or price is None:
        return False
    if price > old_center:  # drifted up → look for resistance (ask) in between
        return any(w.get("persistence", 0) >= PERSIST_MIN
                   and old_center < w["price"] < price
                   for w in rec.get("ask_walls", []))
    if price < old_center:  # drifted down → look for support (bid) in between
        return any(w.get("persistence", 0) >= PERSIST_MIN
                   and price < w["price"] < old_center
                   for w in rec.get("bid_walls", []))
    return False


def recentre_coincidence(ob):
    print("\n── Recentre × order-book coincidence (Phase-1 seed) ──────────")
    if len(ob) < 30:
        print("  Not enough book history to join against recentres yet.")
        return
    try:
        sys.path.insert(0, HERE)
        import backtest as bt
    except Exception as e:  # noqa: BLE001
        print(f"  Could not import backtest.py ({e}); skipping join.")
        return
    if not os.path.exists(ENGINE_LOG):
        print("  engine_log.jsonl not found; skipping join.")
        return

    since = ob[0]["ts"] - 60
    cy = bt.load_cycles(ENGINE_LOG, since)
    if len(cy) < 30:
        print("  Not enough overlapping engine cycles; skipping join.")
        return
    fills = bt.load_fills(FILLS_LOG, since) if os.path.exists(FILLS_LOG) else []
    idx = bt.assign_fills_to_cycles(cy, fills)
    fbc = {}
    for f, i in idx:
        fbc.setdefault(i, []).append(f)
    events = bt.detect_recenters(cy)
    n = len(cy)

    def fwd(ei):
        end = min(ei + FWD_CYCLES, n - 1)
        return sum(len(fbc.get(j, [])) for j in range(ei + 1, end + 1))

    # cross-tab: blocking durable wall present? × dud?
    tab = {(True, True): 0, (True, False): 0, (False, True): 0, (False, False): 0}
    joined = 0
    for ei in events:
        if ei == 0:
            continue
        rec = _nearest_ob(cy[ei]["ts"], ob)
        if rec is None:
            continue  # recentre predates the collector
        joined += 1
        blocked = _blocking_wall(rec, cy[ei - 1]["center"], cy[ei]["price"])
        dud = fwd(ei) < DUD_FILLS
        tab[(blocked, dud)] += 1

    if joined < MIN_RECENTRES:
        print(f"  Only {joined} recentre(s) overlap the book history (need ≥{MIN_RECENTRES}).")
        print("  This is expected right after deploy — revisit in ~1-2 weeks.")
        return

    bt_, bf = tab[(True, True)], tab[(True, False)]
    nt, nf = tab[(False, True)], tab[(False, False)]
    blocked_total = bt_ + bf
    print(f"  joined recentres: {joined}")
    print(f"  blocking durable wall present: {blocked_total}  ({_pct(blocked_total, joined)})")
    print( "                          dud    productive")
    print(f"    wall in the way:     {bt_:4d}    {bf:5d}")
    print(f"    clear path:          {nt:4d}    {nf:5d}")
    if blocked_total:
        print(f"  → when a wall blocked, {_pct(bt_, blocked_total)} were duds "
              f"(veto would have suppressed these)")
    if (nt + nf):
        print(f"  → when path was clear, {_pct(nt, nt + nf)} were still duds "
              f"(other dud causes remain)")
    print("  Read: high left-column dud-rate + the veto sparing productive recentres")
    print("  is the green light to wire the order-book veto into the recentre gate.")


def main():
    print(f"=== Order-book report {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} ===")
    ob = _load_ob()
    if collector_health(ob):
        recentre_coincidence(ob)


if __name__ == "__main__":
    main()
