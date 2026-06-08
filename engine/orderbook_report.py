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
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OB_LOG = os.path.join(HERE, "orderbook_log.jsonl")

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


def _evaluate(ob, side, events):
    """For each wall event, look forward FWD_CYCLES: did price approach the wall,
    and if so did it HOLD (reverse) or BREAK through?"""
    n = len(ob)
    approached = held = broke = 0
    for i, W, atr in events:
        end = min(i + FWD_CYCLES, n - 1)
        fut = [ob[j]["price"] for j in range(i + 1, end + 1) if ob[j].get("price")]
        if not fut:
            continue
        touch = TOUCH_TOL_ATR * atr
        brk = BREAK_TOL_ATR * atr
        if side == "bid":   # support below — approach = price dips to it
            reached = min(fut) <= W + touch
            through = min(fut) < W - brk
        else:               # resistance above — approach = price rises to it
            reached = max(fut) >= W - touch
            through = max(fut) > W + brk
        if reached:
            approached += 1
            broke += 1 if through else 0
            held += 0 if through else 1
    return approached, held, broke


def phase2_wall_respect(ob):
    print("\n── Phase-2 validation: do durable walls act as boundaries? ───")
    if len(ob) < 200:
        print("  Not enough book history yet (need ≥200 cycles).")
        return
    avail_bid = sum(1 for r in ob if _durable_anchorable(r, "bid_walls")[0]) / len(ob)
    avail_ask = sum(1 for r in ob if _durable_anchorable(r, "ask_walls")[0]) / len(ob)

    tot_appr = tot_held = tot_broke = 0
    for side in ("bid", "ask"):
        ev = _wall_events(ob, side)
        appr, held, broke = _evaluate(ob, side, ev)
        tot_appr += appr; tot_held += held; tot_broke += broke
        hr = held / appr if appr else 0.0
        label = "support (bid)" if side == "bid" else "resistance (ask)"
        print(f"  {label:16s} walls={len(ev):3d}  approached={appr:3d}  "
              f"held={held:3d} broke={broke:3d}  hold-rate={_pct(held, appr)}")

    print(f"  anchorable wall within ±{ANCHOR_ATR}×ATR of mid — "
          f"bid: {_pct(int(avail_bid*len(ob)), len(ob))}  ask: {_pct(int(avail_ask*len(ob)), len(ob))}")

    hold_rate = tot_held / tot_appr if tot_appr else 0.0
    avail_min = min(avail_bid, avail_ask)
    print(f"  combined hold-rate: {_pct(tot_held, tot_appr)} over {tot_appr} approaches")

    # ── Data-driven verdict (no hard-coded optimism) ──────────────────────────
    print("  VERDICT:", end=" ")
    if tot_appr < MIN_EVENTS:
        print(f"INSUFFICIENT DATA — only {tot_appr} wall approaches "
              f"(need ≥{MIN_EVENTS}). Keep collecting.")
    elif hold_rate >= HOLD_RATE_GREEN and avail_min >= AVAIL_GREEN:
        print("SUPPORTED — durable walls hold often enough and sit near the grid "
              "boundary frequently enough to anchor to. Build Phase 2.")
    elif hold_rate < HOLD_RATE_GREEN:
        print(f"NOT SUPPORTED — walls hold only {hold_rate*100:.0f}% of the time "
              f"(need ≥{HOLD_RATE_GREEN*100:.0f}%); price slices through them too "
              f"often to anchor boundaries safely.")
    else:
        print(f"WEAK — walls hold {hold_rate*100:.0f}% but a durable wall is in "
              f"anchor range only {avail_min*100:.0f}% of cycles "
              f"(need ≥{AVAIL_GREEN*100:.0f}%); anchoring would rarely apply.")


def main():
    print(f"=== Order-book report {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} ===")
    ob = _load_ob()
    if collector_health(ob):
        phase2_wall_respect(ob)


if __name__ == "__main__":
    main()
