#!/usr/bin/env python3
"""Order-book collector report — Phase 0 analysis hook (read-only).

Two jobs, both read-only:

1. **Collector health** — confirm orderbook.py is gathering usable data: cycle
   count, span, cadence, how often a *persistent* wall is present, the
   persistence distribution, and book imbalance / depth span.

2. **Phase-2 validation: do durable walls act as price boundaries?** Range
   anchoring (Phase 2) only makes sense if a persistent wall is a level price
   tends to *respect* (reverse at) rather than slice straight through. For every
   distinct durable wall that price later approaches, this measures whether price
   HELD at it (reversed) or BROKE through — plus how often a durable wall even
   sits within anchoring range of mid. The verdict is **computed from those
   numbers**, not hard-coded.

   (The earlier Phase-1 "recentre veto" question was killed by the data: walls
   almost never sat between the old centre and the recentre target, and the duds
   were on clear paths. See git history / HANDOFF. We don't test it here anymore.)

Nothing here changes trading logic. Usage on the droplet:
    venv/bin/python orderbook_report.py
"""
import bisect
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OB_LOG = os.path.join(HERE, "orderbook_log.jsonl")
ENGINE_LOG = os.path.join(HERE, "engine_log.jsonl")
JOIN_TOL_S = 90    # max gap when matching an order-book ts to an engine cycle

# ── Phase-2 tunables ─────────────────────────────────────────────────────────
PERSIST_MIN     = 3      # cycles a wall must survive to count as "durable"
ANCHOR_ATR      = 1.5    # a wall is "anchorable" if within this many ATR of mid
TOUCH_TOL_ATR   = 0.25   # price "approaches" a wall within this band
BREAK_TOL_ATR   = 0.25   # price "breaks" a wall if it pushes past by this band
FWD_CYCLES      = 30     # ~60 min forward window to judge hold vs break
STALE_GAP       = 2      # wall absent this many cycles → a later return is a NEW event

# Verdict thresholds (data-driven gate for building Phase 2)
HOLD_RATE_GREEN = 0.60   # ≥ this share of approached walls must HOLD (reverse)
AVAIL_GREEN     = 0.40   # ≥ this share of cycles must have a wall within anchor range
MIN_EVENTS      = 15     # below this, sample too small to call


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


def _durable_anchorable(r, side):
    """Durable walls on `side` within ANCHOR_ATR of mid, as [(price, atr)]."""
    out = []
    atr = r.get("atr") or 0
    for w in r.get(side, []):
        if w.get("persistence", 0) < PERSIST_MIN:
            continue
        da = w.get("dist_atr")
        if da is not None and abs(da) <= ANCHOR_ATR:
            out.append(w["price"])
    return out, atr


def _wall_events(ob, side):
    """Distinct durable-wall appearances within anchor range. A wall (price
    bucket) that vanishes for > STALE_GAP cycles and returns counts as new."""
    side_key = "bid_walls" if side == "bid" else "ask_walls"
    tracked = {}          # price -> last cycle index seen
    events = []           # (i, wall_price, atr_at_i)
    for i, r in enumerate(ob):
        prices, atr = _durable_anchorable(r, side_key)
        for W in prices:
            if W not in tracked:
                events.append((i, W, atr or (r.get("price", 0) * 0.01)))
            tracked[W] = i
        # drop walls not seen recently so a later reappearance is a fresh event
        for W in [w for w, last in tracked.items() if i - last > STALE_GAP]:
            del tracked[W]
    return events


def _event_outcome(ob, side, i, W, atr):
    """Look forward FWD_CYCLES from event i: None if price never approached the
    wall, else 'held' (reversed) or 'broke' (sliced through)."""
    n = len(ob)
    end = min(i + FWD_CYCLES, n - 1)
    fut = [ob[j]["price"] for j in range(i + 1, end + 1) if ob[j].get("price")]
    if not fut:
        return None
    touch = TOUCH_TOL_ATR * atr
    brk = BREAK_TOL_ATR * atr
    if side == "bid":       # support below — approach = price dips to it
        if min(fut) > W + touch:
            return None
        return "broke" if min(fut) < W - brk else "held"
    else:                   # resistance above — approach = price rises to it
        if max(fut) < W - touch:
            return None
        return "broke" if max(fut) > W + brk else "held"


def _regime_lookup(ob):
    """Build ts→regime by joining engine_log.jsonl cycles to order-book
    timestamps. Returns a state_of(ts) function, or None if unavailable.
    Buckets: 'trending_down' / 'trending_up' / 'RANGE' (per the engine's own
    trend flags — the convention used everywhere else)."""
    if not (os.path.exists(ENGINE_LOG) and ob):
        return None
    try:
        sys.path.insert(0, HERE)
        import backtest as bt
        cy = bt.load_cycles(ENGINE_LOG, ob[0]["ts"] - 60)
    except Exception as e:  # noqa: BLE001
        print(f"  (regime join unavailable: {e})")
        return None
    if len(cy) < 30:
        return None
    ts_arr = [c["ts"] for c in cy]

    def state_of(ts):
        j = bisect.bisect_left(ts_arr, ts)
        best, bd = None, JOIN_TOL_S + 1
        for k in (j - 1, j):
            if 0 <= k < len(cy):
                d = abs(cy[k]["ts"] - ts)
                if d < bd:
                    best, bd = cy[k], d
        if best is None or bd > JOIN_TOL_S:
            return None
        if best.get("trending_down"):
            return "trending_down"
        if best.get("trending_up"):
            return "trending_up"
        return "RANGE"

    return state_of


def _verdict(hold, appr):
    if appr < MIN_EVENTS:
        return f"INSUFFICIENT ({appr} approaches, need ≥{MIN_EVENTS})"
    hr = hold / appr
    if hr >= HOLD_RATE_GREEN:
        return f"SUPPORTED ({hr*100:.0f}% ≥ {HOLD_RATE_GREEN*100:.0f}%)"
    return f"NOT SUPPORTED ({hr*100:.0f}% < {HOLD_RATE_GREEN*100:.0f}%)"


def phase2_wall_respect(ob):
    print("\n── Phase-2 validation: do durable walls hold, by regime? ─────")
    if len(ob) < 200:
        print("  Not enough book history yet (need ≥200 cycles).")
        return
    state_of = _regime_lookup(ob)
    if state_of is None:
        print("  No engine_log regime join — reporting un-segmented only.")

    # tally[(regime, side)] = [approached, held, broke, n_walls]
    tally = defaultdict(lambda: [0, 0, 0, 0])
    overall = [0, 0, 0]   # appr, held, broke
    for side in ("bid", "ask"):
        for i, W, atr in _wall_events(ob, side):
            regime = (state_of(ob[i]["ts"]) if state_of else "ALL") or "unknown"
            cell = tally[(regime, side)]
            cell[3] += 1
            o = _event_outcome(ob, side, i, W, atr)
            if o is None:
                continue
            overall[0] += 1; overall[1 if o == "held" else 2] += 1
            cell[0] += 1; cell[1 if o == "held" else 2] += 1

    print(f"  {'regime':14s} {'side':16s} {'walls':>5s} {'appr':>5s} "
          f"{'held':>5s} {'broke':>5s} {'hold':>6s}")
    order = ["RANGE", "trending_up", "trending_down", "unknown", "ALL"]
    seen = sorted(tally.keys(),
                  key=lambda k: (order.index(k[0]) if k[0] in order else 99, k[1]))
    for regime, side in seen:
        appr, held, broke, walls = tally[(regime, side)]
        label = "support (bid)" if side == "bid" else "resistance (ask)"
        print(f"  {regime:14s} {label:16s} {walls:5d} {appr:5d} "
              f"{held:5d} {broke:5d} {_pct(held, appr):>6s}")

    print(f"  OVERALL hold-rate: {_pct(overall[1], overall[0])} "
          f"over {overall[0]} approaches")

    # ── Verdict — focused on RANGE (the regime where the grid is most active
    #    and where boundary-anchoring would actually apply). Per side, since
    #    the downtrend sample showed support and resistance behave differently.
    print("\n  VERDICT (per regime+side; build anchoring only where SUPPORTED):")
    for regime in ("RANGE", "trending_up", "trending_down"):
        for side in ("bid", "ask"):
            if (regime, side) not in tally:
                continue
            appr, held, broke, _ = tally[(regime, side)]
            label = "support" if side == "bid" else "resistance"
            print(f"    {regime:14s} {label:11s} → {_verdict(held, appr)}")


def main():
    print(f"=== Order-book report {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} ===")
    ob = _load_ob()
    if collector_health(ob):
        phase2_wall_respect(ob)


if __name__ == "__main__":
    main()
