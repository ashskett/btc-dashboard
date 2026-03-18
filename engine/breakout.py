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
SPIKE_UP_MIN_MOVE   = 4.0    # Volatility-spike UP requires price to have moved at least
                              # this many ATRs from the 10-bar low before firing.
                              # Prevents single fast candles (e.g. +$600 spike) from
                              # shutting down inner+mid — only large sustained pumps
                              # (≥4×ATR ≈ $1,700 at current ATR) trigger BREAKOUT_UP
                              # via this layer. DOWN is unchanged (capital protection).
PROXIMITY_ATR_MULT  = 1.0    # proximity alert when within ATR × this of grid edge
EXHAUSTION_AVG_MULT = 0.20   # 5-candle avg move < ATR×this → momentum stalling (was 0.05 — never triggered)
EXHAUSTION_WINDOW   = 5      # candles to average for exhaustion check
POST_CLEAR_COOLDOWN_DOWN = 3600  # seconds after DOWN exhaustion before either layer can re-fire (~12 cycles / 60 min).
                                  # The DOWN re-arm loop (4 consecutive dips in a ranging market) is driven by
                                  # the momentum layer, so both layers must be blocked for DOWN after exhaustion.
POST_CLEAR_COOLDOWN_UP   = 600   # seconds after UP exhaustion before spike layer can re-fire (2 engine cycles).
                                  # Momentum is still exempt for UP — genuine collapses should still fire.
POST_CLEAR_COOLDOWN = POST_CLEAR_COOLDOWN_UP  # backward-compat alias

# Delayed inner reentry during sustained BREAKOUT_UP
# After INNER_REENTRY_CYCLES active cycles (price still elevated, momentum fading but not yet
# fully exhausted), the inner bot is brought back online while mid stays off and outer keeps running.
# This recovers fills during the consolidation phase before full exhaustion triggers a redeploy.
INNER_REENTRY_CYCLES   = 3    # min cycles of active BREAKOUT_UP before inner is eligible (≈15 min)
INNER_REENTRY_AVG_MULT = 0.5  # 5-candle avg move < ATR×this → momentum fading enough for inner reentry
                               # (softer than EXHAUSTION_AVG_MULT=0.20; reentry fires before full stall)
                              # Prevents the exhaustion → immediate-re-fire loop: the spike layer sees the same
                              # elevated ATR/BB that triggered the original breakout and re-fires on the very
                              # next cycle before market conditions have changed.  Momentum layer is exempt —
                              # a genuine 4-bar collapse should still fire even inside the cooldown window.

# ── Liquidity sweep guard ──────────────────────────────────────────────────────
# A breakout fires as PENDING_UP / PENDING_DOWN first.  The engine takes NO action
# on a pending state.  Confirmation requires a NEW 1H candle to have closed (tracked
# via candle timestamp) with price still on the correct side of fire_price.
# A 5-minute liquidity sweep will reverse before the next hourly close; a real
# breakout will still be above/below fire_price when the new candle prints.
#
# SWEEP_CONFIRM_ATR_MARGIN — how far price may retrace from fire_price and still
# confirm.  0.5×ATR ≈ ~$150 at current levels; covers normal noise without letting
# a genuine reversal through.
SWEEP_GUARD_ENABLED       = True   # set False to revert to immediate-fire behaviour
SWEEP_CONFIRM_ATR_MARGIN  = 0.50   # price may pull back at most this many ATRs from
                                    # fire_price before the pending state is cancelled

# Wick quality filter for the volatility-spike layer.
# For BREAKOUT_UP: the triggering candle must close in the upper WICK_BODY_MIN_RATIO
# of its high-low range.  Liquidity sweeps produce a spike wick that closes well
# below the high; genuine breakout candles close near their highs.
# Symmetric check applied to DOWN sweeps.
WICK_BODY_CLOSE_RATIO = 0.45   # close must be in top/bottom 45 % of candle range


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
            "cycles_active": 0,
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
        price = df.close.iloc[-1]
        mid   = df.close.tail(5).mean()

        # Wick quality check — liquidity sweeps leave long wicks; real breakout
        # candles close near their extreme.  If the candle has a meaningful range,
        # require the close to be in the upper (UP) or lower (DOWN) portion.
        try:
            candle_hi = float(df.high.iloc[-1])
            candle_lo = float(df.low.iloc[-1])
            candle_range = candle_hi - candle_lo
            if candle_range > 0:
                close_pos = (price - candle_lo) / candle_range  # 0 = low, 1 = high
                if price > mid and close_pos < WICK_BODY_CLOSE_RATIO:
                    # Spike UP but close is in bottom half of candle — wick, likely sweep
                    print(f"BREAKOUT_UP (spike) suppressed — wick close "
                          f"({close_pos:.2f} of range, need >{WICK_BODY_CLOSE_RATIO:.2f})")
                    return None
                if price <= mid and close_pos > (1 - WICK_BODY_CLOSE_RATIO):
                    # Spike DOWN but close is in top half of candle — wick, likely sweep
                    print(f"BREAKOUT_DOWN (spike) suppressed — wick close "
                          f"({close_pos:.2f} of range, need <{1-WICK_BODY_CLOSE_RATIO:.2f})")
                    return None
        except Exception:
            pass  # df.high/low may not exist on older test data; skip silently

        if price > mid:
            # UP: only fire if price has moved a large distance from recent lows.
            # Filters out single-candle spikes that elevate ATR without a real breakout.
            move_from_low = price - df.close.tail(10).min()
            if move_from_low >= atr * SPIKE_UP_MIN_MOVE:
                return "UP"
            return None
        return "DOWN"

    return None


# ── Sweep guard: pending-state confirmation ────────────────────────────────────

def _pending_direction(active: str | None) -> str | None:
    """Return 'UP' or 'DOWN' if active is a PENDING state, else None."""
    if isinstance(active, str) and active.startswith("PENDING_"):
        return active.split("_", 1)[1]
    return None


def _try_confirm_pending(df) -> str | None:
    """
    Called when active state is PENDING_UP or PENDING_DOWN.

    Waits for a NEW 1H candle to close (detected via candle timestamp stored at
    fire time) then checks whether price is still on the correct side of fire_price
    within SWEEP_CONFIRM_ATR_MARGIN × ATR.

    Returns the confirmed direction ("UP"/"DOWN") on success, None if still waiting
    or if the move was a liquidity sweep (price reversed).
    """
    s = _load_state()
    direction = _pending_direction(s.get("active"))
    if not direction:
        return None

    fire_price = s.get("fire_price", 0.0)
    atr        = float(df.atr.iloc[-1])
    price_now  = float(df.close.iloc[-1])

    # Candle-timestamp gate — only confirm once a new 1H candle has closed.
    # This ensures a sub-hourly liquidity sweep can't confirm itself on the same
    # candle that triggered the detection.
    try:
        current_ts = int(df.index[-1].timestamp())
    except Exception:
        current_ts = int(time.time())

    pending_ts = s.get("pending_candle_ts", 0)
    if current_ts <= pending_ts:
        # Same candle still open — keep waiting
        return None

    # New candle has closed — check price is still on the right side
    margin = atr * SWEEP_CONFIRM_ATR_MARGIN
    if direction == "UP":
        confirmed = price_now >= fire_price - margin
    else:  # DOWN
        confirmed = price_now <= fire_price + margin

    if confirmed:
        print(f"Breakout {direction} CONFIRMED after sweep guard "
              f"(fire=${fire_price:,.0f}, now=${price_now:,.0f}, "
              f"margin=±${margin:,.0f})")
        s["active"] = direction
        _save_state(s)
        return direction
    else:
        print(f"Breakout {direction} CANCELLED — liquidity sweep detected "
              f"(fire=${fire_price:,.0f}, now=${price_now:,.0f}, "
              f"reversed by ${abs(price_now - fire_price):,.0f}, "
              f"allowance=${margin:,.0f})")
        s["active"]            = None
        s["fire_price"]        = None
        s["consec_up"]         = 0
        s["consec_down"]       = 0
        s["cycles_active"]     = 0
        s["cleared_at"]        = time.time()
        s["cleared_direction"] = direction
        _save_state(s)
        return None


# ── Public API ─────────────────────────────────────────────────────────────────

def breakout_detected(df, atr_window: int = 30,
                      regime: str = None, gap_ratio: float = 0.0) -> str | None:
    """
    Returns "UP", "DOWN", or None.

    Checks layers in priority order — first match wins.
    Does NOT re-fire while a breakout state is already active
    (call clear_breakout_state() after redeployment).

    regime     — current engine regime string ("TREND_UP", "RANGE", etc.)
    gap_ratio  — (price - trendline) / ATR; positive = price above trendline

    Guards applied:
      • regime == "TREND_UP"  → suppress ALL UP signals (already positioned; inner
        already off via trending_up logic; stopping mid achieves nothing and costs fills)
      • gap_ratio > 3.0       → suppress DOWN from volatility spike only. At this
        distance above the trendline, elevated ATR/BB from the prior move generates
        false DOWN signals. Layer 1 momentum still fires (needs 4 bars × 2.5×ATR ≈
        $1,400 drop) — that's a genuine collapse, not noise.

    atr_window kept as param for backward-compat but not used by layer 1.
    """
    s = _load_state()

    # ── Sweep guard: handle PENDING state ──────────────────────────────────────
    # A pending breakout is waiting for one confirming 1H candle close.
    # Keep updating momentum counters so we don't lose the streak count while
    # waiting.  Return the confirmed direction (or None) without running new
    # detection — we already committed to the signal, just confirming it held.
    if _pending_direction(s.get("active")):
        _check_momentum(df)
        return _try_confirm_pending(df)

    # Don't re-fire while already in a confirmed active breakout
    if s.get("active") in ("UP", "DOWN"):
        # Still update momentum counters so we don't lose the count
        _check_momentum(df)
        return None

    # Run both layers independently so guards can be applied selectively
    mom_dir   = _check_momentum(df)
    spike_dir = _check_volatility_spike(df, atr_window)

    # Guard 1: in an established TREND_UP, UP breakouts are redundant.
    # The engine is already in the correct posture (inner off, mid+outer on via
    # trending_up logic). Re-firing stops mid for 5-15 min on every trend impulse.
    if regime == "TREND_UP":
        if mom_dir == "UP":
            print(f"BREAKOUT_UP (momentum) suppressed — regime already TREND_UP")
            mom_dir = None
        if spike_dir == "UP":
            print(f"BREAKOUT_UP (spike) suppressed — regime already TREND_UP")
            spike_dir = None

    # Guard 2: in a strong uptrend (gap_ratio > 3.0 = trending_up territory),
    # suppress DOWN from the volatility spike layer.  After a sustained trend move
    # ATR and BB width are naturally elevated — spike layer fires on normal pullbacks.
    # Momentum layer still fires if there is a genuine 4-bar collapse.
    if gap_ratio > 3.0 and spike_dir == "DOWN" and mom_dir != "DOWN":
        print(f"BREAKOUT_DOWN (spike) suppressed — gap_ratio={gap_ratio:.1f}x, "
              f"no momentum confirmation (requires 4 bars × {MOMENTUM_ATR_MULT}×ATR)")
        spike_dir = None

    # Guard 3: post-exhaustion cooldown.
    # After a DOWN exhaustion, the momentum layer immediately re-arms on any 4
    # consecutive down-closes in a ranging market — causing the None→PENDING→DOWN
    # loop observed in the logs.  For DOWN: block BOTH layers for 60 min (~12 cycles).
    # For UP: block spike only for 10 min (2 cycles) — existing behaviour.
    s3 = _load_state()
    cleared_at  = s3.get("cleared_at")
    cleared_dir = s3.get("cleared_direction")
    if cleared_at:
        elapsed  = time.time() - cleared_at
        cooldown = POST_CLEAR_COOLDOWN_DOWN if cleared_dir == "DOWN" else POST_CLEAR_COOLDOWN_UP
        remaining = int(cooldown - elapsed)
        if elapsed < cooldown:
            if cleared_dir == "DOWN":
                # Block both layers for DOWN to prevent momentum-driven re-arm loop
                if mom_dir == "DOWN":
                    print(f"BREAKOUT_DOWN (momentum) suppressed — post-exhaustion cooldown "
                          f"({int(elapsed)}s elapsed, {remaining}s remaining / {cooldown//60:.0f}min total)")
                    mom_dir = None
                if spike_dir == "DOWN":
                    print(f"BREAKOUT_DOWN (spike) suppressed — post-exhaustion cooldown "
                          f"({int(elapsed)}s elapsed, {remaining}s remaining / {cooldown//60:.0f}min total)")
                    spike_dir = None
            else:
                # UP cooldown: block spike only; momentum still fires for genuine collapse
                if spike_dir:
                    print(f"BREAKOUT {spike_dir} (spike) suppressed — post-exhaustion cooldown "
                          f"({int(elapsed)}s elapsed, {remaining}s remaining)")
                    spike_dir = None

    direction = mom_dir or spike_dir

    if direction:
        s2 = _load_state()          # re-read after _check_momentum wrote
        fire_price = float(df.close.iloc[-1])

        if SWEEP_GUARD_ENABLED:
            # Park in PENDING — engine takes no bot action until confirmed
            try:
                candle_ts = int(df.index[-1].timestamp())
            except Exception:
                candle_ts = int(time.time())

            s2["active"]            = f"PENDING_{direction}"
            s2["fire_price"]        = fire_price
            s2["fired_at"]          = time.time()
            s2["pending_candle_ts"] = candle_ts
            s2["consec_up"]         = 0
            s2["consec_down"]       = 0
            _save_state(s2)
            print(f"Breakout {direction} PENDING sweep guard "
                  f"(fire=${fire_price:,.0f}) — awaiting next 1H close to confirm")
            return None   # engine holds bots; confirmation fires on next new candle
        else:
            s2["active"]     = direction
            s2["fire_price"] = fire_price
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
    # PENDING states are handled by _try_confirm_pending; exhaustion doesn't apply
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
            s["consec_up"] = 0; s["consec_down"] = 0; s["cycles_active"] = 0
            s["cleared_at"] = time.time(); s["cleared_direction"] = "DOWN"
            _save_state(s)
            return True
        if direction == "UP" and price < fire:
            print(f"Breakout UP cancelled — price ${price:,.0f} fell back below fire ${fire:,.0f}")
            s["active"] = None; s["fire_price"] = None
            s["consec_up"] = 0; s["consec_down"] = 0; s["cycles_active"] = 0
            s["cleared_at"] = time.time(); s["cleared_direction"] = "UP"
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
        s["active"]            = None
        s["fire_price"]        = None
        s["consec_up"]         = 0
        s["consec_down"]       = 0
        s["cycles_active"]     = 0
        s["cleared_at"]        = time.time()
        s["cleared_direction"] = direction
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
    Stamps cleared_at so the post-exhaustion cooldown applies to the next cycle.
    """
    s = _load_state()
    prev_direction = s.get("active") or s.get("cleared_direction")
    _save_state({
        "consec_up": 0, "consec_down": 0,
        "active": None, "fire_price": None,
        "cycles_active": 0,
        "cleared_at": time.time(),
        "cleared_direction": prev_direction,
    })


def increment_active_cycles():
    """
    Increment cycles_active counter in breakout state.
    Called each engine cycle while BREAKOUT_UP is confirmed active.
    Used to gate the delayed inner-bot reentry check.
    """
    s = _load_state()
    s["cycles_active"] = s.get("cycles_active", 0) + 1
    _save_state(s)


def breakout_inner_ready(df) -> bool:
    """
    Returns True when it is safe to re-enable the inner bot during a sustained
    BREAKOUT_UP, before full exhaustion triggers a grid redeploy.

    Conditions (all must hold):
      1. cycles_active >= INNER_REENTRY_CYCLES  — breakout has been active long enough
         to rule out a short squeeze / fake-out.  Default 3 cycles ≈ 15 minutes.
      2. price >= fire_price  — still elevated.  If price has fallen back below the
         original fire level the move has reversed; inner should stay off until the
         full exhaustion path handles the redeploy.
      3. 5-candle avg_move < ATR × INNER_REENTRY_AVG_MULT  — momentum is fading.
         Default mult 0.5 (softer than the EXHAUSTION_AVG_MULT=0.20 full-stall check),
         so reentry fires while the market is consolidating, not yet dead.

    If all three hold the engine sets inner=ON, mid=OFF, outer=ON.
    Full exhaustion (breakout_exhausting()) still fires later and triggers the
    normal grid redeploy at the new price level.
    """
    s = _load_state()
    if s.get("active") != "UP":
        return False

    cycles = s.get("cycles_active", 0)
    if cycles < INNER_REENTRY_CYCLES:
        return False

    fire_price = s.get("fire_price") or 0.0
    price_now  = float(df.close.iloc[-1])
    if fire_price and price_now < fire_price:
        # Price fell back below fire — not the right time; let exhaustion handle it
        return False

    if len(df) < EXHAUSTION_WINDOW + 1:
        return False

    moves = [
        abs(df.close.iloc[-i] - df.close.iloc[-i - 1])
        for i in range(1, EXHAUSTION_WINDOW + 1)
    ]
    avg_move = statistics.mean(moves)
    atr      = float(df.atr.iloc[-1])
    return avg_move < atr * INNER_REENTRY_AVG_MULT