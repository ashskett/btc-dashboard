"""
breakout.py  —  Directional breakout detection for the grid engine.

WHY THE OLD DETECTOR FAILED
────────────────────────────
The original detector fired only when ATR > baseline×1.8 OR BB_width > baseline×2.
Analysis of the 03-12→03-13 log shows a $3,392 move where ATR peaked at only 1.26×
baseline — the old detector was completely blind to the move. That move was a
classic slow-grind / staircase breakout, not a volatility spike.

NEW DETECTION LAYERS (in priority order)
─────────────────────────────────────────
1. MOMENTUM (primary, catches slow grinds)
   N consecutive closes in same direction AND total move over those N bars > ATR×1.0.
   Calibrated against the log: streak≥4 + move>1.0×ATR fires exactly once on the
   real breakout (03-13 00:25, +$1,042), zero false positives.

2. VOLATILITY SPIKE (secondary, catches flash moves)
   ATR > baseline×1.5 (was 1.8) — tightened since layer 1 now handles slow moves.
   Direction assigned from recent price midpoint.

3. PROXIMITY ALERT (warning only, not a circuit breaker)
   Price within ATR×1.0 of outer grid edge. Lets engine widen the outer bot
   pre-emptively before a boundary is actually breached.

DIRECTION
─────────
Every detection now returns "UP", "DOWN", or None — never a plain bool.
The engine responds asymmetrically:
  UP   →  stop inner+mid, keep/widen outer bot, log BREAKOUT_UP state,
          wait for exhaustion then redeploy grid at new level
  DOWN →  stop all bots (capital protection)

EXHAUSTION DETECTION
────────────────────
After a breakout fires, breakout_exhausting() detects when the move is stalling
(5-candle avg move < ATR×0.05) so the engine can redeploy the grid at the new
price level rather than sitting idle in cash.

PERSISTENCE
───────────
Consecutive-close count is persisted in breakout_state.json so the engine
doesn't lose count across the 5-minute cycle gaps.
"""

import os
import json
import time
import statistics

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "breakout_state.json")

BREAKOUT_MAX_AGE = 4 * 3600   # auto-expire active breakout state after 4 hours

# ── Tuning constants ───────────────────────────────────────────────────────────
MOMENTUM_STREAK     = 4      # consecutive same-direction closes required
MOMENTUM_ATR_MULT   = 2.5    # total move over those N bars must exceed ATR × this
                              # 1.5 fired on a 4H $455 grind (1.71×ATR, 0.7% move) —
                              # inner grid half-width is ~2.7×ATR so price hadn't even
                              # threatened the grid boundary. 2.5 requires ~$755 at current
                              # ATR; a normal ranging day won't reach it. Mar 12-13 $3,392
                              # dump (~8×ATR total) fires with ease.
VOLATILITY_MULT     = 1.7    # ATR spike multiplier (was 1.8, tested 1.5)
BB_MULT             = 2.0    # BB width spike multiplier (unchanged)
PROXIMITY_ATR_MULT  = 1.0    # proximity alert when within ATR × this of grid edge
EXHAUSTION_AVG_MULT = 0.20   # 5-candle avg move < ATR×this → momentum stalling (was 0.05 — never triggered)
EXHAUSTION_WINDOW   = 5      # candles to average for exhaustion check


def _load_state() -> dict:
    try:
        s = json.load(open(_STATE_FILE))
        # Auto-expire stale active breakout states so they don't block trading
        # across restarts or long quiet periods.
        if s.get("active") and s.get("fired_at"):
            age = time.time() - s["fired_at"]
            if age > BREAKOUT_MAX_AGE:
                print(f"Breakout state expired after {age/3600:.1f}h — auto-clearing")
                s = {"consec_up": 0, "consec_down": 0, "active": None, "fire_price": None}
                _save_state(s)
        return s
    except Exception:
        return {
            "consec_up":    0,
            "consec_down":  0,
            "active":       None,   # None / "UP" / "DOWN"
            "fire_price":   None,
        }


def _save_state(s: dict):
    try:
        json.dump(s, open(_STATE_FILE, "w"))
    except Exception:
        pass


# ── Layer 1: momentum (slow-grind breakouts) ──────────────────────────────────

def _check_momentum(df) -> str | None:
    """
    N consecutive closes in the same direction AND total move > ATR × mult.
    Persists consecutive count across engine cycles.
    """
    s = _load_state()

    price_now  = df.close.iloc[-1]
    price_prev = df.close.iloc[-2] if len(df) > 1 else price_now

    if price_now > price_prev:
        s["consec_up"]   += 1
        s["consec_down"]  = 0
    elif price_now < price_prev:
        s["consec_down"] += 1
        s["consec_up"]    = 0

    atr         = df.atr.iloc[-1]
    move_N_up   = df.close.iloc[-1] - df.close.iloc[-MOMENTUM_STREAK] if len(df) >= MOMENTUM_STREAK else 0
    move_N_down = df.close.iloc[-MOMENTUM_STREAK] - df.close.iloc[-1] if len(df) >= MOMENTUM_STREAK else 0
    min_move    = atr * MOMENTUM_ATR_MULT

    direction = None
    if s["consec_up"] >= MOMENTUM_STREAK and move_N_up >= min_move:
        direction = "UP"
    elif s["consec_down"] >= MOMENTUM_STREAK and move_N_down >= min_move:
        direction = "DOWN"

    _save_state(s)
    return direction


# ── Layer 2: volatility spike (flash crashes / news candles) ──────────────────

def _check_volatility_spike(df, window: int = 30) -> str | None:
    atr     = df.atr.iloc[-1]
    atr_avg = df.atr.tail(window).mean()
    bb      = df.bb_width.iloc[-1]
    bb_avg  = df.bb_width.tail(window).mean()

    if atr > atr_avg * VOLATILITY_MULT or bb > bb_avg * BB_MULT:
        # Assign direction from recent price vs short-term midpoint
        price    = df.close.iloc[-1]
        mid      = df.close.tail(5).mean()
        return "UP" if price > mid else "DOWN"

    return None


# ── Public API ─────────────────────────────────────────────────────────────────

def breakout_detected(df, atr_window: int = 30) -> str | None:
    """
    Returns "UP", "DOWN", or None.

    Checks layers in priority order — first match wins.
    Does NOT re-fire while a breakout state is already active
    (call clear_breakout_state() after redeployment).

    atr_window kept as param for backward-compat but not used by layer 1.
    """
    s = _load_state()

    # Don't re-fire while already in an active breakout
    if s.get("active") in ("UP", "DOWN"):
        # Still update momentum counters so we don't lose the count
        _check_momentum(df)
        return None

    direction = _check_momentum(df) or _check_volatility_spike(df, atr_window)

    if direction:
        s2 = _load_state()          # re-read after _check_momentum wrote
        s2["active"]     = direction
        s2["fire_price"] = float(df.close.iloc[-1])
        s2["fired_at"]   = time.time()
        s2["consec_up"]  = 0
        s2["consec_down"] = 0
        _save_state(s2)

    return direction


def breakout_exhausting(df) -> bool:
    """
    Returns True when an active breakout's momentum is stalling —
    5-candle average move drops below ATR × EXHAUSTION_AVG_MULT.

    Also returns True if price has recovered back past the fire price
    (false positive detection — the "breakout" reversed immediately).

    Engine should use this to trigger grid redeployment at the new price level.
    Automatically clears active breakout state when exhaustion is confirmed.
    """
    s = _load_state()
    if s.get("active") not in ("UP", "DOWN"):
        return False

    if len(df) < EXHAUSTION_WINDOW + 1:
        return False

    price     = df.close.iloc[-1]
    fire      = s.get("fire_price")
    direction = s.get("active")

    # Price recovery check — if price has moved back past the fire price,
    # the breakout was a false positive and we should redeploy immediately.
    if fire:
        if direction == "DOWN" and price > fire:
            print(f"Breakout DOWN cancelled — price ${price:,.0f} recovered above fire ${fire:,.0f}")
            s["active"] = None; s["fire_price"] = None
            s["consec_up"] = 0; s["consec_down"] = 0
            _save_state(s)
            return True
        if direction == "UP" and price < fire:
            print(f"Breakout UP cancelled — price ${price:,.0f} fell back below fire ${fire:,.0f}")
            s["active"] = None; s["fire_price"] = None
            s["consec_up"] = 0; s["consec_down"] = 0
            _save_state(s)
            return True

    moves = [
        abs(df.close.iloc[-i] - df.close.iloc[-i - 1])
        for i in range(1, EXHAUSTION_WINDOW + 1)
    ]
    avg_move  = statistics.mean(moves)
    atr       = df.atr.iloc[-1]
    stalling  = avg_move < atr * EXHAUSTION_AVG_MULT

    if stalling:
        s["active"]      = None
        s["fire_price"]  = None
        s["consec_up"]   = 0
        s["consec_down"] = 0
        _save_state(s)

    return stalling


def proximity_alert(df, grid_low: float, grid_high: float) -> str | None:
    """
    Warning (not a circuit breaker) — returns "UP", "DOWN", or None when
    price is within ATR×PROXIMITY_ATR_MULT of the outer grid edge.

    Engine uses this to widen the outer bot pre-emptively.
    """
    if not grid_low or not grid_high:
        return None
    price = df.close.iloc[-1]
    atr   = df.atr.iloc[-1]
    if price > grid_high - (atr * PROXIMITY_ATR_MULT):
        return "UP"
    if price < grid_low + (atr * PROXIMITY_ATR_MULT):
        return "DOWN"
    return None


def get_breakout_state() -> dict:
    """Return current breakout state dict for logging."""
    return _load_state()


def clear_breakout_state():
    """
    Reset all breakout state.
    Call this after a successful grid redeployment at the new price level.
    """
    _save_state({
        "consec_up": 0, "consec_down": 0,
        "active": None, "fire_price": None,
    })