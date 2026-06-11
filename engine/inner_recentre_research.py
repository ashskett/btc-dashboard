#!/usr/bin/env python3
"""Read-only research: how often does the INNER tier sit idle out-of-range during
RANGE, for how long, with how much harvestable swing — to size the "recentre just
inner" idea and find the trigger threshold.

Scenario (Ash): price drifts off the top (or bottom) of the tight inner grid while
mid/outer still bracket it. We don't full-recentre (would disrupt mid/outer), so
inner sits idle. Question: how big is that forgone opportunity, and only in RANGE
(where inner is ON — it's already turned OFF during trending_up)?

Streams engine_log.jsonl (per-cycle price + regime + tier ranges) and fills_log.
Nothing here trades or writes. Run: venv/bin/python inner_recentre_research.py
"""
import bisect
import json
import os
from collections import Counter
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_LOG = os.path.join(HERE, "engine_log.jsonl")
FILLS_LOG = os.path.join(HERE, "fills_log.jsonl")

SWING_W = 12          # ~30-min rolling window for harvestable swing
FEE_FLOOR_PCT = 0.6   # round-trip fee floor — chop below this isn't harvestable
MIN_EP = 4            # episode must last >= this many cycles (~10 min) to count
JOIN_TOL_S = 180      # fill→cycle time-join tolerance


def _parse_t(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def main():
    cyc = []   # (ts, price, regime, inner_low, inner_high)
    for line in open(ENGINE_LOG):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts, price, regime = d.get("ts"), d.get("price"), d.get("regime")
        tiers = d.get("tiers") or []
        if not (ts and price and tiers):
            continue
        inner = tiers[0]
        ilo, ihi = inner.get("grid_low"), inner.get("grid_high")
        if ilo is None or ihi is None:
            continue
        cyc.append((ts, float(price), regime, float(ilo), float(ihi)))
    cyc.sort(key=lambda c: c[0])
    n = len(cyc)
    if n < 100:
        print("not enough engine_log data")
        return
    prices = [c[1] for c in cyc]
    cadence = (cyc[-1][0] - cyc[0][0]) / max(n - 1, 1)
    days = (cyc[-1][0] - cyc[0][0]) / 86400

    def state(i):
        ts, price, regime, ilo, ihi = cyc[i]
        if regime != "RANGE":
            return "non-range"
        if price > ihi:
            return "above"
        if price < ilo:
            return "below"
        return "inside"

    st = [state(i) for i in range(n)]

    def swing(i):
        w = prices[max(0, i - SWING_W):i + 1]
        return (max(w) - min(w)) / prices[i] * 100 if prices[i] else 0

    rng = [i for i in range(n) if cyc[i][2] == "RANGE"]
    nr = len(rng)
    above = sum(1 for i in rng if st[i] == "above")
    below = sum(1 for i in rng if st[i] == "below")
    inside = sum(1 for i in rng if st[i] == "inside")

    # contiguous RANGE + inner-out episodes
    episodes = []
    i = 0
    while i < n:
        if cyc[i][2] == "RANGE" and st[i] in ("above", "below"):
            j = i
            while j < n and cyc[j][2] == "RANGE" and st[j] in ("above", "below"):
                j += 1
            episodes.append((i, j - 1))
            i = j
        else:
            i += 1
    long_eps = [(a, b) for a, b in episodes if (b - a + 1) >= MIN_EP]
    durs = sorted(b - a + 1 for a, b in long_eps)
    # peak harvestable swing within each long episode
    ep_amps = [max(swing(k) for k in range(a, b + 1)) for a, b in long_eps]
    harvestable = sum(1 for amp in ep_amps if amp >= FEE_FLOOR_PCT)

    def pctl(xs, p):
        return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else 0

    # fills landing during inner-out RANGE cycles
    cts = [c[0] for c in cyc]
    fills = []
    for line in open(FILLS_LOG):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("source") == "profits":
            continue
        te = _parse_t(d.get("time"))
        if te is None:
            continue
        fills.append((te, d.get("bot_index"), d.get("side")))
    innerout_fills = Counter()
    innerout_total = 0
    for te, bi, side in fills:
        k = bisect.bisect_left(cts, te)
        best, bd = None, JOIN_TOL_S + 1
        for kk in (k - 1, k):
            if 0 <= kk < n and abs(cts[kk] - te) < bd:
                best, bd = kk, abs(cts[kk] - te)
        if best is None or bd > JOIN_TOL_S:
            continue
        if cyc[best][2] == "RANGE" and st[best] in ("above", "below"):
            innerout_total += 1
            innerout_fills[bi] += 1

    cyc_days = lambda c: c * cadence / 86400
    print(f"=== Inner-recentre opportunity — {days:.0f}d, {n} cycles, "
          f"~{cadence/60:.1f}min/cycle ===\n")
    print(f"RANGE cycles: {nr} ({cyc_days(nr):.0f}d)  — where inner is ON")
    print(f"  price vs inner grid:  inside {100*inside/nr:.0f}%  |  "
          f"ABOVE-top {100*above/nr:.0f}%  |  below-bottom {100*below/nr:.0f}%")
    print(f"  → inner idle out-of-range {100*(above+below)/nr:.0f}% of RANGE time "
          f"(~{cyc_days(above+below):.1f}d)\n")
    print(f"Sustained episodes (>= {MIN_EP} cyc / ~{MIN_EP*cadence/60:.0f}min): "
          f"{len(long_eps)}")
    if long_eps:
        print(f"  duration cycles: median {pctl(durs,0.5)}  p90 {pctl(durs,0.9)}  "
              f"max {durs[-1]}  ({cyc_days(durs[-1]):.1f}d longest)")
        print(f"  total idle time in episodes: {cyc_days(sum(durs)):.1f}d")
        print(f"  peak swing within episode: median {pctl(sorted(ep_amps),0.5):.2f}% "
              f"(fee floor {FEE_FLOOR_PCT}%)")
        print(f"  episodes with harvestable chop (peak swing >= floor): "
              f"{harvestable}/{len(long_eps)} ({100*harvestable/len(long_eps):.0f}%)")
    print(f"\nFills that landed while inner was idle-out-of-range (RANGE): "
          f"{innerout_total}")
    print(f"  by tier — inner {innerout_fills.get(0,0)}  mid {innerout_fills.get(1,0)}"
          f"  outer {innerout_fills.get(2,0)}")
    print("  (mid/outer fills here = activity at a level inner could re-harvest;")
    print("   inner fills here should be ~0 since it's out of range)")


if __name__ == "__main__":
    main()
