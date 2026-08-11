"""zero_mode.py — GRIDDY ZERO: the static grid reset (Ash approved 2026-07-31).

WHY: a month of data showed the adaptive layer was the loss: 35 full-grid
redeploys in ~10 days, 65% of turnover was the engine repositioning its own base
(systematically buy-high/sell-low around inventory-band flips), true 30d P&L
−$1,972 while a dumb static grid over the same 2-min price path made +$415.
The inventory-band system unwinds the grid's NATURAL positioning (BTC-heavy at
lows, BTC-light at highs) at the worst moments. Verdict: the grid's edge is
patience; the engine fidgeted it away.

WHAT ZERO MODE DOES (while active):
  KEEP  — one static nested grid, deployed ONCE (holdings-preserving, zero-trade);
          all three bots always ON; key-level price targets fully live (DOWN
          SmartTrade protection, UP DCA — risk lives there, Ash's doctrine);
          every bit of observability (status, fills, realpnl, slide, audit,
          watchdog, heartbeat, true P&L).
  STOP  — drift recentres, inventory SELL_ONLY/BUY_ONLY intensive modes, weekend
          mode, regime/trend bot switching, breakout auto-response, flash-move
          bot stops, BUY_ONLY chase, knife brake, ride mode. All demoted to
          observe/log only. The ladder RESTS.
  HUMAN — if price closes outside the static range for >RANGE_EXIT_HOURS, Ash
          gets a Telegram alert with a proposed re-centred range and a one-click
          approve link (/zero/approve). Nothing moves without him.

State: zero_mode.json {active, tiers, activated_at, outside_since,
pending_proposal, last_range_alert}.
"""
import os
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "zero_mode.json")

RANGE_EXIT_HOURS = 2.0     # price outside the grid this long → propose re-centre
RANGE_ALERT_EVERY_H = 6.0  # re-alert cadence while still outside


def _load():
    try:
        return json.load(open(STATE_FILE))
    except FileNotFoundError:
        return {"active": False}
    except Exception:
        # Corrupt state must NOT silently disable zero mode (2026-08-11 incident:
        # truncated file -> adaptive layer woke unnoticed). Fail SAFE: treat as
        # active-with-no-tiers (ladder holds, range checks skip) and scream.
        try:
            import notify
            notify.notify_critical("ZERO MODE STATE FILE CORRUPT — holding static "
                                   "posture; repair zero_mode.json / re-activate.")
        except Exception:
            pass
        return {"active": True, "tiers": [], "corrupt": True}


def _save(s):
    """ATOMIC write (2026-08-12): a plain json.dump was truncated mid-write on
    2026-08-11 20:35, corrupting the state file — _load() then failed and
    is_active() silently returned False, waking the ENTIRE adaptive layer for
    2+ hours (SELL_ONLY entered at 96%). tmp+rename is atomic on POSIX: readers
    see the old file or the new file, never a partial one."""
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def is_active():
    return bool(_load().get("active"))


def get_state():
    return _load()


def make_static_tiers(price):
    """Nested static tiers centred near `price`, spans mirroring the classic
    inner/mid/outer widths, level counts kept comfortably above the fee floor
    (min_step ≈ 0.6% of price; steps below are 0.9–3%)."""
    p = round(price, -2)
    tiers = [
        {"name": "inner", "grid_low": p - 2000, "grid_high": p + 2000, "levels": 10},
        {"name": "mid",   "grid_low": p - 3500, "grid_high": p + 3500, "levels": 9},
        {"name": "outer", "grid_low": p - 5000, "grid_high": p + 5000, "levels": 6},
    ]
    for t in tiers:
        w = t["grid_high"] - t["grid_low"]
        t["step"] = round(w / (t["levels"] - 1), 2)
        t["grid_levels"] = [round(t["grid_low"] + i * t["step"], 2)
                            for i in range(t["levels"])]
        t["fee_ok"] = t["step"] >= price * 0.004 * 1.5
    return tiers


def activate(tiers):
    s = _load()
    s.update({"active": True, "tiers": tiers, "activated_at": time.time(),
              "outside_since": None, "pending_proposal": None,
              "last_range_alert": 0.0})
    _save(s)
    return s


def deactivate():
    s = _load()
    s["active"] = False
    _save(s)
    return s


def grid_bounds(s=None):
    s = s or _load()
    tiers = s.get("tiers") or []
    if not tiers:
        return None, None
    return (min(t["grid_low"] for t in tiers), max(t["grid_high"] for t in tiers))


def check_range_exit(price):
    """Call each cycle while active. Returns an alert string when price has been
    outside the static range > RANGE_EXIT_HOURS (rate-limited); stores a proposed
    re-centred grid for one-click approval. Never trades."""
    s = _load()
    if not s.get("active"):
        return None
    lo, hi = grid_bounds(s)
    if lo is None:
        return None
    now = time.time()
    inside = lo <= price <= hi
    if inside:
        if s.get("outside_since"):
            s["outside_since"] = None
            _save(s)
        return None
    if not s.get("outside_since"):
        s["outside_since"] = now
        _save(s)
        return None
    hours_out = (now - s["outside_since"]) / 3600
    if hours_out < RANGE_EXIT_HOURS:
        return None
    if now - (s.get("last_range_alert") or 0) < RANGE_ALERT_EVERY_H * 3600:
        return None
    proposal = make_static_tiers(price)
    s["pending_proposal"] = proposal
    s["last_range_alert"] = now
    _save(s)
    side = "ABOVE" if price > hi else "BELOW"
    plo, phi = min(t["grid_low"] for t in proposal), max(t["grid_high"] for t in proposal)
    return ("GRIDDY ZERO: price ${:,.0f} has been {} the static grid "
            "(${:,.0f}–${:,.0f}) for {:.1f}h. Proposed new range ${:,.0f}–${:,.0f} "
            "centred on price. Approve: http://100.94.227.121:5050/zero/approve"
            "?token=dbf92fff8e0baf1c856ea590d74cd640a556a037ddd12369 — or ignore "
            "to keep the current grid.").format(price, side, lo, hi, hours_out,
                                                plo, phi)


def take_pending_proposal():
    """Pop the stored proposal (used by /zero/approve)."""
    s = _load()
    p = s.get("pending_proposal")
    if p:
        s["pending_proposal"] = None
        _save(s)
    return p
