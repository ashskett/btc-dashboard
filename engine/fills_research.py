#!/usr/bin/env python3
"""Read-only research: grid productivity & realized swing amplitude by PRICE
LEVEL and REGIME.

Tests the observation that the grid harvests better at the range lows (large
relief-rally amplitude → spikes cross sell levels) than at the highs (swings
compressed below the grid step). Splits by regime so we can separate "genuinely
fewer oscillations" from "engine was defensively off (TREND_DOWN)".

Sources (both append-only logs already on the droplet):
  portfolio_log.jsonl — per-cycle btc_price + regime (time-at-price + amplitude)
  fills_log.jsonl     — real fills + 'profits' round-trip records

Nothing here trades or writes. Run: venv/bin/python fills_research.py
"""
import json
import os
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.join(HERE, "portfolio_log.jsonl")
FILLS = os.path.join(HERE, "fills_log.jsonl")

BUCKET = 2000          # $ price bucket
SWING_W = 12           # rolling window (~30 min) for realized swing amplitude
FEE_FLOOR_PCT = 0.6    # round-trip fee floor — grid can't profit on swings below this


def _bkt(p):
    return int(p // BUCKET * BUCKET)


def main():
    rows = []
    for line in open(PORT):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        p, t = d.get("btc_price"), d.get("ts")
        if p and t:
            rows.append((t, p, d.get("regime")))
    rows.sort(key=lambda r: r[0])
    ts = [r[0] for r in rows]; px = [r[1] for r in rows]; rg = [r[2] for r in rows]
    n = len(rows)
    if n < 100:
        print("not enough portfolio data"); return
    cadence = (ts[-1] - ts[0]) / max(n - 1, 1)

    cyc = collections.Counter()
    cyc_range = collections.Counter()
    swing_sum = collections.defaultdict(float); swing_n = collections.Counter()
    swing_sum_r = collections.defaultdict(float); swing_n_r = collections.Counter()

    for i in range(n):
        b = _bkt(px[i]); cyc[b] += 1
        is_range = (rg[i] == "RANGE")
        if is_range:
            cyc_range[b] += 1
        win = px[max(0, i - SWING_W):i + 1]
        amp = (max(win) - min(win)) / px[i] * 100 if px[i] else 0
        swing_sum[b] += amp; swing_n[b] += 1
        if is_range:
            swing_sum_r[b] += amp; swing_n_r[b] += 1

    prof = collections.defaultdict(collections.Counter)   # bucket -> side count
    real = collections.defaultdict(collections.Counter)
    for line in open(FILLS):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        p = d.get("price")
        if not p:
            continue
        b = _bkt(p); side = d.get("side", "?")
        if d.get("source") == "profits":
            prof[b][side] += 1
        else:
            real[b][side] += 1

    days = (ts[-1] - ts[0]) / 86400
    print(f"cadence ~{cadence/60:.1f} min/cycle | {n} cycles | {days:.0f} days "
          f"| fee floor ~{FEE_FLOOR_PCT:.2f}% round-trip")
    print(f"{'bucket($k)':>10} {'cyc':>6} {'%RANGE':>6} {'days':>5} "
          f"{'swing%(all)':>11} {'swing%(RNG)':>11} {'profits':>7} "
          f"{'prof/RNGday':>11} {'realfills':>9}")
    for b in sorted(cyc):
        c = cyc[b]; cr = cyc_range[b]
        sw = swing_sum[b] / swing_n[b] if swing_n[b] else 0
        swr = swing_sum_r[b] / swing_n_r[b] if swing_n_r[b] else 0
        pr = sum(prof[b].values()); rf = sum(real[b].values())
        bday = c * cadence / 86400               # days price spent in bucket
        rday = cr * cadence / 86400              # RANGE-days in bucket
        prate = pr / rday if rday > 0.05 else 0  # profit round-trips per RANGE-day
        print(f"{b//1000:>4}-{(b+BUCKET)//1000:<5} {c:>6} {100*cr/c:>5.0f}% "
              f"{bday:>5.1f} {sw:>11.2f} {swr:>11.2f} {pr:>7} "
              f"{prate:>11.1f} {rf:>9}")

    # ── Headline: RANGE-only, low band vs high band ──────────────────────────
    def band(lo, hi):
        cyc_r = sum(cyc_range[b] for b in cyc_range if lo <= b < hi)
        swsum = sum(swing_sum_r[b] for b in swing_sum_r if lo <= b < hi)
        swnn = sum(swing_n_r[b] for b in swing_n_r if lo <= b < hi)
        prc = sum(sum(prof[b].values()) for b in prof if lo <= b < hi)
        rdays = cyc_r * cadence / 86400
        return (swsum / swnn if swnn else 0,
                prc / rdays if rdays > 0.05 else 0, rdays, prc)

    print("\nRANGE-only comparison (controls for regime):")
    for name, lo, hi in [("LOW  58-64k", 58000, 64000),
                         ("MID  64-70k", 64000, 70000),
                         ("HIGH 70-84k", 70000, 84000)]:
        sw, prate, rdays, prc = band(lo, hi)
        below = " (below fee floor!)" if sw < FEE_FLOOR_PCT else ""
        print(f"  {name}: mean swing {sw:.2f}%{below} | "
              f"{prc} profit round-trips over {rdays:.1f} RANGE-days "
              f"= {prate:.1f}/day")


if __name__ == "__main__":
    main()
