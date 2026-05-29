#!/usr/bin/env python3
"""backtest.py — read-only historical analysis of grid engine logs.

NON-TRADING. This tool never touches 3Commas, never starts/stops bots, and
never writes engine state. It only reads the append-only logs the engine
already produces and reports findings, to make the daily-review items
data-led rather than guesswork:

  trend-down : Replay the trending_down Schmitt trigger over the logged
               gap_ratio series under different entry/exit thresholds, and
               measure the trade-off — inner+mid fills foregone vs. adverse
               price drift avoided. (Daily-review: "Backtest trend_down
               triggers against fills so mode changes are data-led.")

  recenter   : Detect every grid recentre event and measure what happened in
               the cycles that followed — fills captured and price drawdown.
               Flags whether recentring is "too twitchy". (Daily-review:
               "Measure recenter events vs subsequent fills and drawdown,
               then add hysteresis if too twitchy.")

  spacing    : In RANGE cycles, compare the inner-tier grid step against
               realised per-cycle price movement and fill cadence, to judge
               whether range-mode spacing is capturing churn. (Daily-review:
               "Review whether range-mode grid spacing is capturing churn
               efficiently.")

Data sources (default: alongside this script on the droplet):
  engine_log.jsonl     one JSON object per ~2-min cycle
  fills_log.jsonl      one JSON object per fill (bot_index 0=Narrow/inner,
                       1=Mid/mid, 2=Wider/outer)

Usage:
  python3 backtest.py trend-down [--sweep]
  python3 backtest.py trend-down --entry -2.0 --exit -1.0
  python3 backtest.py recenter [--window 30]
  python3 backtest.py spacing
  python3 backtest.py all
  python3 backtest.py <mode> --logdir /root/grid-engine --since 2026-05-01

Pure standard library. Streams the (large) engine log line-by-line.
"""

import argparse
import json
import os
import statistics
from bisect import bisect_right
from datetime import datetime, timezone

# Deployed trending_down Schmitt thresholds (kept in sync with regime.py).
DEPLOYED_ENTRY = -2.0
DEPLOYED_EXIT = -1.0

# Candidate threshold pairs for --sweep. (entry, exit); entry < exit <= 0.
SWEEP_PAIRS = [
    (-1.5, -0.5),
    (-1.5, -1.0),
    (-2.0, -1.0),   # deployed
    (-2.0, -1.5),
    (-2.5, -1.0),
    (-2.5, -1.5),
    (-3.0, -2.0),
]

_BOT_NAME = {0: "Narrow", 1: "Mid", 2: "Wider"}


# ── Loading ──────────────────────────────────────────────────────────────

def _parse_iso(s):
    """Parse a 3Commas ISO timestamp like '2026-03-23T19:52:08.806Z' to epoch."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        # Fall back: strip fractional seconds
        try:
            base = s.split(".")[0]
            if "+" not in base:
                base += "+00:00"
            return datetime.fromisoformat(base).timestamp()
        except ValueError:
            return None


def load_cycles(path, since_epoch=None):
    """Stream the engine log, keeping only the slim fields the analyses need.

    Returns a list of dicts ordered by ts. The 37k-line / 76 MB log is read
    line-by-line so memory stays bounded to the slim records.
    """
    cycles = []
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = d.get("ts")
            if ts is None:
                continue
            if since_epoch is not None and ts < since_epoch:
                continue
            tiers = d.get("tiers") or []
            inner = tiers[0] if len(tiers) > 0 else {}
            cycles.append({
                "ts": ts,
                "dt": d.get("dt"),
                "price": d.get("price"),
                "atr": d.get("atr"),
                "regime": d.get("regime"),
                "gap_ratio": d.get("gap_ratio"),
                "trending_down": bool(d.get("trending_down", False)),
                "trending_up": bool(d.get("trending_up", False)),
                "compression": bool(d.get("compression", False)),
                "center": d.get("center"),
                "drift_triggered": bool(d.get("drift_triggered", False)),
                "inventory_mode": d.get("inventory_mode"),
                "inner_step": inner.get("step"),
                "inner_min_step": inner.get("min_step"),
                "inner_fee_ok": inner.get("fee_ok"),
            })
    cycles.sort(key=lambda c: c["ts"])
    return cycles


def load_fills(path, since_epoch=None):
    """Load fills with parsed epoch time, ordered by time."""
    fills = []
    if not os.path.exists(path):
        return fills
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = _parse_iso(d.get("time"))
            if t is None:
                continue
            if since_epoch is not None and t < since_epoch:
                continue
            fills.append({
                "t": t,
                "bot_index": d.get("bot_index"),
                "side": d.get("side"),
                "price": d.get("price"),
                "qty": d.get("qty"),
            })
    fills.sort(key=lambda f: f["t"])
    return fills


def assign_fills_to_cycles(cycles, fills):
    """For each fill, find the index of the cycle that was active at fill time
    (the last cycle whose ts <= fill time). Returns list of (fill, cycle_idx)."""
    cycle_ts = [c["ts"] for c in cycles]
    out = []
    for f in fills:
        idx = bisect_right(cycle_ts, f["t"]) - 1
        if idx < 0:
            continue
        out.append((f, idx))
    return out


# ── Helpers ──────────────────────────────────────────────────────────────

def _span_days(cycles):
    if len(cycles) < 2:
        return 0.0
    return (cycles[-1]["ts"] - cycles[0]["ts"]) / 86400.0


def _fmt_pct(x):
    return f"{x*100:+.3f}%" if x is not None else "n/a"


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


# ── Mode: trend-down ───────────────────────────────────────────────────────

def replay_trending_down(cycles, entry, exit_):
    """Replay the Schmitt trigger over the gap_ratio series.

    Mirrors regime.trend_strength exactly: once below `entry`, stays True until
    gap_ratio recovers above `exit_`. Returns a list[bool] aligned to cycles.
    """
    down = False
    states = []
    for c in cycles:
        g = c["gap_ratio"]
        if g is None:
            states.append(down)
            continue
        if down:
            down = g < exit_
        else:
            down = g < entry
        states.append(down)
    return states


def analyse_trend_down(cycles, fills_idx, entry, exit_):
    """Return metrics for one (entry, exit) threshold pair."""
    states = replay_trending_down(cycles, entry, exit_)
    n = len(cycles)
    off_cycles = sum(1 for s in states if s)

    # Forward 1-cycle return for each cycle (price[i+1] vs price[i]).
    fwd = [None] * n
    for i in range(n - 1):
        p0, p1 = cycles[i]["price"], cycles[i + 1]["price"]
        if p0 and p1:
            fwd[i] = (p1 - p0) / p0

    # Adverse drift avoided while inner+mid are OFF: sum of forward returns
    # during off cycles (negative total = the policy kept tight grids out of a
    # falling market). Also the worst single forward drop avoided.
    off_fwd = [fwd[i] for i in range(n) if states[i] and fwd[i] is not None]
    drift_while_off = sum(off_fwd) if off_fwd else 0.0
    worst_off_drop = min(off_fwd) if off_fwd else 0.0

    # Inner/mid fills that land inside an off-window = fills this policy would
    # forgo relative to always-on (these only exist where the *real* policy had
    # bots on, i.e. where this candidate is stricter than deployed).
    innermid_fills_in_off = 0
    for f, idx in fills_idx:
        if f["bot_index"] in (0, 1) and 0 <= idx < n and states[idx]:
            innermid_fills_in_off += 1

    # Validation: how closely does the replay match the *logged* trending_down
    # flag. At the deployed thresholds this should be ~100%.
    match = sum(1 for i in range(n) if states[i] == cycles[i]["trending_down"])

    return {
        "entry": entry,
        "exit": exit_,
        "off_cycles": off_cycles,
        "off_fraction": off_cycles / n if n else 0.0,
        "innermid_fills_in_off": innermid_fills_in_off,
        "drift_while_off": drift_while_off,
        "worst_off_drop": worst_off_drop,
        "match_logged_pct": match / n if n else 0.0,
    }


def run_trend_down(cycles, fills_idx, args):
    print("\n=== TREND-DOWN TRIGGER ANALYSIS ===")
    days = _span_days(cycles)
    print(f"Window: {cycles[0]['dt']} -> {cycles[-1]['dt']}  ({len(cycles)} cycles, {days:.1f} days)")
    innermid_total = sum(1 for f, _ in fills_idx if f["bot_index"] in (0, 1))
    print(f"Total inner+mid (Narrow+Mid) fills in window: {innermid_total}")
    print("\nReading the columns:")
    print("  off%        = share of cycles inner+mid would be paused by this trigger")
    print("  fills_off   = inner+mid fills that fall inside off-windows (forgone vs always-on)")
    print("  drift_off   = summed forward price drift while off (negative = downside avoided)")
    print("  worst_drop  = worst single-cycle drop avoided while off")
    print("  match%      = agreement with the logged trending_down flag (sanity)\n")

    pairs = SWEEP_PAIRS if args.sweep else [(args.entry, args.exit)]
    header = f"{'entry':>6} {'exit':>6} {'off%':>7} {'fills_off':>10} {'drift_off':>11} {'worst_drop':>11} {'match%':>7}"
    print(header)
    print("-" * len(header))
    for (e, x) in pairs:
        m = analyse_trend_down(cycles, fills_idx, e, x)
        tag = "  <- deployed" if (e == DEPLOYED_ENTRY and x == DEPLOYED_EXIT) else ""
        print(f"{e:>6.1f} {x:>6.1f} {m['off_fraction']*100:>6.1f}% "
              f"{m['innermid_fills_in_off']:>10d} {_fmt_pct(m['drift_while_off']):>11} "
              f"{_fmt_pct(m['worst_off_drop']):>11} {m['match_logged_pct']*100:>6.1f}%{tag}")

    if args.sweep:
        print("\nInterpretation: a stricter trigger (more negative entry) pauses inner+mid")
        print("less often (lower off%) and forgoes fewer fills, but avoids less downside.")
        print("A looser trigger protects more but sacrifices churn fills. Pick the pair")
        print("that avoids meaningful downside (negative drift_off) while keeping fills_off low.")


# ── Mode: recenter ─────────────────────────────────────────────────────────

def detect_recenters(cycles, eps=1.0):
    """Return indices where the grid centre moved meaningfully vs the prior
    cycle (a recentre/redeploy). eps in price units guards float jitter."""
    events = []
    prev = None
    for i, c in enumerate(cycles):
        ctr = c["center"]
        if ctr is None:
            continue
        if prev is not None and abs(ctr - prev) > eps:
            events.append(i)
        prev = ctr
    return events


def run_recenter(cycles, fills_idx, args):
    print("\n=== RECENTER EVENT ANALYSIS ===")
    days = _span_days(cycles)
    window = args.window
    events = detect_recenters(cycles)
    n = len(cycles)
    print(f"Window: {cycles[0]['dt']} -> {cycles[-1]['dt']}  ({n} cycles, {days:.1f} days)")
    if not events:
        print("No recentre events detected.")
        return
    rate = len(events) / days if days else 0.0
    print(f"Recentre events: {len(events)}  ({rate:.2f}/day)")
    print(f"Look-ahead window: {window} cycles (~{window*2} min)\n")

    # Pre-index fills by cycle for fast windowed counts.
    fills_by_cycle = {}
    for f, idx in fills_idx:
        fills_by_cycle.setdefault(idx, []).append(f)

    fills_per_event = []
    drawdowns = []
    twitchy = 0
    for ei in events:
        ctr = cycles[ei]["center"]
        end = min(ei + window, n - 1)
        # Fills in (ei, end]
        fc = 0
        for j in range(ei + 1, end + 1):
            fc += len(fills_by_cycle.get(j, []))
        fills_per_event.append(fc)
        if fc < 2:
            twitchy += 1
        # Max adverse excursion from the new centre over the window.
        lows = [cycles[j]["price"] for j in range(ei, end + 1) if cycles[j]["price"]]
        if ctr and lows:
            mae = (min(lows) - ctr) / ctr
            drawdowns.append(mae)

    med_fills = _median(fills_per_event)
    mean_fills = statistics.mean(fills_per_event) if fills_per_event else 0
    med_dd = _median(drawdowns)
    print(f"Fills in {window}-cycle window after each recentre:")
    print(f"  median={med_fills}  mean={mean_fills:.2f}  "
          f"max={max(fills_per_event)}  total={sum(fills_per_event)}")
    print(f"Recentres followed by <2 fills (low payoff): {twitchy}/{len(events)} "
          f"({twitchy/len(events)*100:.0f}%)")
    print(f"Median max-adverse-excursion from new centre: {_fmt_pct(med_dd)}")
    if drawdowns:
        print(f"Worst post-recentre drawdown: {_fmt_pct(min(drawdowns))}")

    print("\nInterpretation: a high share of recentres with <2 subsequent fills,")
    print("combined with non-trivial post-recentre drawdown, indicates twitchy")
    print("recentring that cancels orders without earning churn — the case for")
    print("widening the drift threshold or adding a stabilisation/hysteresis gate.")


# ── Mode: spacing ──────────────────────────────────────────────────────────

def run_spacing(cycles, fills_idx, args):
    print("\n=== RANGE-MODE GRID SPACING ANALYSIS ===")
    n = len(cycles)
    range_idx = [i for i in range(n) if cycles[i]["regime"] == "RANGE"]
    if not range_idx:
        print("No RANGE cycles in window.")
        return
    days = _span_days(cycles)
    range_days = days * (len(range_idx) / n) if n else 0.0
    print(f"Window: {cycles[0]['dt']} -> {cycles[-1]['dt']}  ({n} cycles, {days:.1f} days)")
    print(f"RANGE cycles: {len(range_idx)} ({len(range_idx)/n*100:.0f}% of cycles, ~{range_days:.1f} days)\n")

    steps = [cycles[i]["inner_step"] for i in range_idx if cycles[i]["inner_step"]]
    atrs = [cycles[i]["atr"] for i in range_idx if cycles[i]["atr"]]
    fee_ok = [cycles[i]["inner_fee_ok"] for i in range_idx if cycles[i]["inner_fee_ok"] is not None]

    if steps:
        print(f"Inner step: median=${_median(steps):,.0f}  "
              f"min=${min(steps):,.0f}  max=${max(steps):,.0f}")
    if steps and atrs:
        ratios = []
        for i in range_idx:
            s, a = cycles[i]["inner_step"], cycles[i]["atr"]
            if s and a:
                ratios.append(s / a)
        if ratios:
            print(f"Inner step / ATR: median={_median(ratios):.2f}x "
                  f"(step relative to 1h volatility)")
    if fee_ok:
        ok = sum(1 for x in fee_ok if x)
        print(f"Inner fee_ok: {ok}/{len(fee_ok)} cycles ({ok/len(fee_ok)*100:.0f}% profitable after fees)")

    # Realised per-cycle move vs step: how often does price move >= one step
    # between cycles (a move large enough to potentially cross a grid line)?
    crosses = 0
    counted = 0
    moves = []
    rset = set(range_idx)
    for i in range_idx:
        if i + 1 >= n:
            continue
        p0, p1, step = cycles[i]["price"], cycles[i + 1]["price"], cycles[i]["inner_step"]
        if p0 and p1 and step:
            move = abs(p1 - p0)
            moves.append(move)
            counted += 1
            if move >= step:
                crosses += 1
    if counted:
        print(f"\nPer-cycle |move| >= inner step: {crosses}/{counted} cycles "
              f"({crosses/counted*100:.1f}%)")
        print(f"Median per-cycle |move|: ${_median(moves):,.0f}")

    innermid_in_range = 0
    for f, idx in fills_idx:
        if f["bot_index"] in (0, 1) and idx in rset:
            innermid_in_range += 1
    if range_days:
        print(f"Inner+mid fills during RANGE: {innermid_in_range} "
              f"({innermid_in_range/range_days:.1f}/day)")

    print("\nInterpretation: if per-cycle moves rarely reach one inner step and")
    print("fills/day in RANGE are low, the inner grid is wider than the churn it")
    print("needs to catch — tightening step (within the fee floor) would capture")
    print("more round-trips. If step/ATR is already near the fee floor, the limiter")
    print("is fees, not spacing.")


# ── CLI ────────────────────────────────────────────────────────────────────

def main(argv=None):
    p = argparse.ArgumentParser(description="Read-only grid engine log backtest/analysis.")
    p.add_argument("mode", choices=["trend-down", "recenter", "spacing", "all"])
    p.add_argument("--logdir", default=os.path.dirname(os.path.abspath(__file__)),
                   help="Directory containing engine_log.jsonl + fills_log.jsonl")
    p.add_argument("--since", default=None,
                   help="Only analyse cycles on/after this date, e.g. 2026-05-01")
    p.add_argument("--entry", type=float, default=DEPLOYED_ENTRY, help="trending_down entry threshold")
    p.add_argument("--exit", dest="exit", type=float, default=DEPLOYED_EXIT, help="trending_down exit threshold")
    p.add_argument("--sweep", action="store_true", help="trend-down: compare a range of thresholds")
    p.add_argument("--window", type=int, default=30, help="recenter: look-ahead window in cycles")
    args = p.parse_args(argv)

    since_epoch = None
    if args.since:
        since_epoch = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()

    engine_log = os.path.join(args.logdir, "engine_log.jsonl")
    fills_log = os.path.join(args.logdir, "fills_log.jsonl")
    if not os.path.exists(engine_log):
        p.error(f"engine_log.jsonl not found in {args.logdir}")

    cycles = load_cycles(engine_log, since_epoch)
    fills = load_fills(fills_log, since_epoch)
    if not cycles:
        p.error("No cycles loaded (check --since / log path).")
    fills_idx = assign_fills_to_cycles(cycles, fills)

    if args.mode in ("trend-down", "all"):
        run_trend_down(cycles, fills_idx, args)
    if args.mode in ("recenter", "all"):
        run_recenter(cycles, fills_idx, args)
    if args.mode in ("spacing", "all"):
        run_spacing(cycles, fills_idx, args)


if __name__ == "__main__":
    main()
