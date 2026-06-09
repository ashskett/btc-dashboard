#!/usr/bin/env python3
"""Weekly automated grid-engine backtest review.

Runs on the droplet via system cron (see install note at the bottom). Imports
the read-only ``backtest.py`` building blocks, computes a handful of robust,
data-led health metrics over a recent window, and — only when a metric breaches
a threshold — files an improvement suggestion onto the AI OS project task list
(deduped) and logs a memory entry.

ANALYSIS ONLY. This script never changes trading logic, never deploys, never
restarts the engine. Its sole side effects are HTTP POSTs to the AI OS API
(create/refresh a planned task; log a memory entry). A human reviews and
approves any actual engine change.

Exit code is always 0 unless the environment is broken (missing logs); a "no
new findings" week is a normal, successful outcome.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ── Config ──────────────────────────────────────────────────────────────────
API_BASE = "https://api.uncrewedmaritime.com"
API_KEY = os.environ.get(
    "ASH_BRAIN_API_KEY",
    "14b4748a62916aba3c28fe00074dab8a9ca39516fed725c574da5001bd542b23",
)
PROJECT = "grid-engine"
HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_LOG = os.path.join(HERE, "engine_log.jsonl")
FILLS_LOG = os.path.join(HERE, "fills_log.jsonl")

WINDOW_DAYS = 14          # lookback for the review
FWD_CYCLES = 30           # ~60 min payoff window after each recentre
DUD_FILLS = 2             # <2 fills in the payoff window = a dud recentre

# The regime-aware recentre gate went live on this date. The trending-state
# recentre checks only count data on/after it, so the review measures the LIVE
# config's effect rather than stale pre-gate behaviour. Update this whenever the
# recentre gate constants change (engine.py _recentre_gate_params).
GATE_DEPLOYED = "2026-05-30"

# Suggestion thresholds (tunable). A suggestion fires only when BOTH the
# volume and the inefficiency bar are cleared, so a quiet/healthy week stays
# silent rather than spamming the dev list.
TREND_RECENTRE_MIN_COUNT = 4      # min trending recentres in window to flag
TREND_DUD_FRAC = 0.70             # ...and this share must be duds
RANGE_RECENTRE_MIN_PER_DAY = 4.0  # min RANGE recentres/day to flag
RANGE_DUD_FRAC = 0.75
SPACING_FEE_OK_FLOOR = 0.90       # flag if <90% of RANGE cycles are fee_ok

DRY_RUN = "--dry-run" in sys.argv  # print metrics + decisions, POST nothing

sys.path.insert(0, HERE)
import backtest as bt  # noqa: E402  (read-only analyser, lives alongside)


# ── HTTP helpers ────────────────────────────────────────────────────────────
def _req(method, path, body=None):
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-Key", API_KEY)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:300]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


def get_existing_task_ids():
    status, data = _req("GET", f"/projects/{PROJECT}")
    if status != 200:
        print(f"  WARN: could not fetch existing tasks (HTTP {status})")
        return set()
    tasks = data.get("tasks") or (data.get("project", {}) or {}).get("tasks") or []
    return {t.get("id") for t in tasks if isinstance(t, dict)}


def file_suggestion(task_id, title, summary, existing_ids):
    """Create a planned task once. Skips if the deterministic id already exists
    (so a previously-filed or already-resolved suggestion is never duplicated
    or silently reopened)."""
    if task_id in existing_ids:
        print(f"  SKIP (already filed): {title}")
        return False
    if DRY_RUN:
        print(f"  [dry-run] WOULD FILE: {title}")
        return False
    body = {
        "id": task_id,
        "title": title,
        "status": "planned",
        "priority": "normal",
        "source": "backtest-weekly",
        "added_by": "weekly-backtest-cron",
        "summary": summary,
        "metadata": {"generated": datetime.now(timezone.utc).isoformat()},
    }
    status, _ = _req("POST", f"/projects/{PROJECT}/tasks/upsert", body)
    ok = status in (200, 201)
    print(f"  {'FILED' if ok else f'FAILED({status})'}: {title}")
    return ok


def log_memory(topic, insight):
    if DRY_RUN:
        print(f"  [dry-run] WOULD LOG MEMORY: {insight[:120]}...")
        return
    _req("POST", "/memory/log", {
        "source": "claude-code",
        "category": "Agent Event",
        "project": PROJECT,
        "topic": topic,
        "insight": insight,
        "text": "Automated weekly backtest review (server-side cron).",
    })


# ── Metrics ─────────────────────────────────────────────────────────────────
def compute():
    if not os.path.exists(ENGINE_LOG):
        print(f"ERROR: {ENGINE_LOG} not found", file=sys.stderr)
        sys.exit(1)
    since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).timestamp()
    cy = bt.load_cycles(ENGINE_LOG, since)
    if len(cy) < 50:
        print("Not enough cycles in window — skipping (need >=50).")
        log_memory(
            f"Weekly backtest review {datetime.now(timezone.utc):%Y-%m-%d}",
            "Skipped: fewer than 50 cycles in the 14-day window.",
        )
        return None
    fi = bt.load_fills(FILLS_LOG, since)
    idx = bt.assign_fills_to_cycles(cy, fi)
    ev = bt.detect_recenters(cy)
    fbc = {}
    for f, i in idx:
        fbc.setdefault(i, []).append(f)
    n = len(cy)
    days = max((cy[-1]["ts"] - cy[0]["ts"]) / 86400.0, 1e-9)

    def fwd(ei, w=FWD_CYCLES):
        end = min(ei + w, n - 1)
        return sum(len(fbc.get(j, [])) for j in range(ei + 1, end + 1))

    def state_of(c):
        if c["trending_down"]:
            return "trending_down"
        if c["trending_up"]:
            return "trending_up"
        return "RANGE"

    # Trending-state checks evaluate the live gate, so they only count recentres
    # on/after the gate deploy date. RANGE (whose threshold the gate didn't
    # touch) uses the full window.
    gate_epoch = datetime.strptime(GATE_DEPLOYED, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp()
    buckets = {"trending_down": [], "trending_up": [], "RANGE": []}
    for ei in ev:
        st = state_of(cy[ei])
        if st in ("trending_down", "trending_up") and cy[ei]["ts"] < gate_epoch:
            continue  # pre-gate trending recentre — not representative of live config
        buckets[st].append(fwd(ei))

    def stats(payoffs):
        tot = len(payoffs)
        duds = sum(1 for p in payoffs if p < DUD_FILLS)
        return tot, duds, (duds / tot if tot else 0.0)

    post_gate_days = max(
        (cy[-1]["ts"] - max(cy[0]["ts"], gate_epoch)) / 86400.0, 1e-9)
    m = {"days": days, "post_gate_days": post_gate_days,
         "recentres": len(ev), "rate": len(ev) / days, "by_state": {}}
    for k, v in buckets.items():
        tot, duds, frac = stats(v)
        m["by_state"][k] = {"count": tot, "duds": duds, "dud_frac": frac}

    rng = [c for c in cy if c["regime"] == "RANGE" and c["inner_fee_ok"] is not None]
    m["range_cycles"] = len(rng)
    m["fee_ok_frac"] = (
        sum(1 for c in rng if c["inner_fee_ok"]) / len(rng) if rng else 1.0
    )
    return m


def build_suggestions(m):
    """Return [(task_id, title, summary)] for breached thresholds only."""
    out = []
    bs = m["by_state"]
    w = WINDOW_DAYS
    pgd = m["post_gate_days"]

    td = bs["trending_down"]
    if td["count"] >= TREND_RECENTRE_MIN_COUNT and td["dud_frac"] >= TREND_DUD_FRAC:
        out.append((
            "auto-recentre-trending-down",
            "Recentre gate: trending_down still recentring with low payoff",
            f"In the {pgd:.1f}d since the recentre gate deployed, the grid "
            f"recentred {td['count']}x during trending_down, "
            f"{td['dud_frac']*100:.0f}% earning <{DUD_FILLS} fills in the next "
            f"~hour. The 2.0x deploy-width safety-valve "
            f"(TREND_DOWN_RECENTRE_EXTREME_MULT) may need raising further, or the "
            f"45-min flood guard lengthening. Investigate before changing.",
        ))

    tu = bs["trending_up"]
    if tu["count"] >= TREND_RECENTRE_MIN_COUNT and tu["dud_frac"] >= TREND_DUD_FRAC:
        out.append((
            "auto-recentre-trending-up",
            "Recentre gate: trending_up recentres still mostly duds",
            f"In the {pgd:.1f}d since the recentre gate deployed, the grid "
            f"recentred {tu['count']}x during trending_up, "
            f"{tu['dud_frac']*100:.0f}% earning <{DUD_FILLS} fills. "
            f"Consider widening TREND_UP_DRIFT_MULT (currently 1.10) or raising "
            f"TREND_UP_CONFIRM_CYCLES (currently 6). Investigate before changing.",
        ))

    rg = bs["RANGE"]
    rg_rate = rg["count"] / m["days"]
    if rg_rate >= RANGE_RECENTRE_MIN_PER_DAY and rg["dud_frac"] >= RANGE_DUD_FRAC:
        out.append((
            "auto-recentre-range",
            "RANGE recentring inefficient — review drift threshold",
            f"Over the last {w}d RANGE recentres ran at {rg_rate:.1f}/day with "
            f"{rg['dud_frac']*100:.0f}% earning <{DUD_FILLS} fills. Consider "
            f"widening the RANGE drift threshold above 0.85x deploy width. "
            f"Investigate before changing.",
        ))

    if m["range_cycles"] >= 50 and m["fee_ok_frac"] < SPACING_FEE_OK_FLOOR:
        out.append((
            "auto-spacing-fee-floor",
            "Range spacing breaching the fee floor",
            f"Over the last {w}d only {m['fee_ok_frac']*100:.0f}% of RANGE cycles "
            f"had a fee-profitable inner step (target >={SPACING_FEE_OK_FLOOR*100:.0f}%). "
            f"Step may be sitting below break-even — review tier level counts / "
            f"min-step guard. Investigate before changing.",
        ))
    return out


def compute_amplitude():
    """Distribution of the live swing-amplitude / fee-floor ratio over the window
    (status.grid_amplitude, logged each cycle). Calibration data for the future
    lean-in / lean-out thresholds — INFORMATIONAL ONLY, files no task. ratio≥1 =
    swings clear the fee floor; 'thin' = sub-floor grind where the grid churns."""
    if not os.path.exists(ENGINE_LOG):
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).timestamp()
    ratios = []
    bands = {"rich": 0, "ok": 0, "thin": 0}
    bands_range = {"rich": 0, "ok": 0, "thin": 0}
    with open(ENGINE_LOG) as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = d.get("ts")
            if not ts or ts < since:
                continue
            ga = d.get("grid_amplitude")
            if not isinstance(ga, dict) or ga.get("ratio") is None:
                continue   # warming / pre-deploy / no reading
            ratios.append(ga["ratio"])
            band = ga.get("band")
            if band in bands:
                bands[band] += 1
                if d.get("regime") == "RANGE":
                    bands_range[band] += 1
    n = len(ratios)
    if n < 50:
        return {"n": n}
    ratios.sort()
    def pctl(p):
        return ratios[min(n - 1, int(p * n))]
    nr = sum(bands_range.values())
    return {
        "n": n, "median": pctl(0.5), "p25": pctl(0.25), "p75": pctl(0.75),
        "bands": bands, "range_n": nr,
        "range_thin_frac": (bands_range["thin"] / nr if nr else 0.0),
    }


def main():
    print(f"=== Weekly backtest review {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} ===")
    m = compute()
    if m is None:
        return
    bs = m["by_state"]
    print(f"Window {m['days']:.1f}d | recentres {m['recentres']} ({m['rate']:.2f}/day)"
          f" | post-gate {m['post_gate_days']:.1f}d")
    for k in ("trending_down", "trending_up", "RANGE"):
        s = bs[k]
        scope = "post-gate" if k != "RANGE" else "full window"
        print(f"  {k:14s} {s['count']:3d} recentres, {s['duds']} duds "
              f"({s['dud_frac']*100:.0f}%)  [{scope}]")
    print(f"  RANGE fee_ok: {m['fee_ok_frac']*100:.0f}% of {m['range_cycles']} cycles")

    # ── Amplitude vs fee floor (informational — calibration data, no task) ────
    amp = compute_amplitude()
    amp_line = ""
    if amp and amp.get("n", 0) >= 50:
        b = amp["bands"]
        print(f"  Amplitude/fee-floor: median ratio {amp['median']:.2f} "
              f"(p25 {amp['p25']:.2f} / p75 {amp['p75']:.2f}) over {amp['n']} reads; "
              f"bands rich/ok/thin = {b['rich']}/{b['ok']}/{b['thin']}; "
              f"RANGE-thin {amp['range_thin_frac']*100:.0f}%")
        amp_line = (f" | amp median {amp['median']:.2f}, "
                    f"RANGE-thin {amp['range_thin_frac']*100:.0f}% (n={amp['n']})")
    elif amp is not None:
        print(f"  Amplitude/fee-floor: only {amp.get('n', 0)} reads — accumulating.")
        amp_line = f" | amp accumulating (n={amp.get('n', 0)})"

    suggestions = build_suggestions(m)
    existing = get_existing_task_ids()
    filed = []
    for tid, title, summary in suggestions:
        if file_suggestion(tid, title, summary, existing):
            filed.append(title)

    if filed:
        insight = (
            f"Recentres {m['recentres']} ({m['rate']:.2f}/day); "
            f"td {bs['trending_down']['count']}/{bs['trending_down']['dud_frac']*100:.0f}%dud, "
            f"tu {bs['trending_up']['count']}/{bs['trending_up']['dud_frac']*100:.0f}%dud, "
            f"range {bs['RANGE']['count']}/{bs['RANGE']['dud_frac']*100:.0f}%dud; "
            f"fee_ok {m['fee_ok_frac']*100:.0f}%. Filed: " + "; ".join(filed)
        )
    else:
        insight = (
            f"No actionable findings. Recentres {m['recentres']} ({m['rate']:.2f}/day); "
            f"td {bs['trending_down']['count']}, tu {bs['trending_up']['count']}, "
            f"range {bs['RANGE']['count']}; fee_ok {m['fee_ok_frac']*100:.0f}%. "
            f"All within thresholds."
        )
    log_memory(f"Weekly backtest review {datetime.now(timezone.utc):%Y-%m-%d}",
               insight + amp_line)
    print(f"Done. {len(filed)} task(s) filed.")


if __name__ == "__main__":
    main()
